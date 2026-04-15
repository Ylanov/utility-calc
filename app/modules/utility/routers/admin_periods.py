# app/modules/utility/routers/admin_periods.py

import asyncio
import logging
from typing import List, Optional
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, func
from fastapi_cache.decorator import cache
from fastapi_cache import FastAPICache
from fastapi_limiter.depends import RateLimiter

from app.core.database import get_db, AsyncSessionLocal
from app.modules.utility.models import User, BillingPeriod, MeterReading, Room
from app.modules.utility.schemas import PeriodCreate, PeriodResponse
from app.core.dependencies import get_current_user, RoleChecker
from app.modules.utility.services.billing import open_new_period
from app.modules.utility.tasks import close_period_task
from app.modules.utility.services.notification_service import send_push_to_all
from app.modules.utility.routers.admin_dashboard import write_audit_log

router = APIRouter(tags=["Admin Periods"])
logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

allow_period_management = RoleChecker(["accountant", "admin"])


async def _safe_clear_cache(namespace: str = "periods"):
    try:
        await FastAPICache.clear(namespace=namespace)
    except Exception as e:
        logger.warning(f"Cache clear failed for '{namespace}': {e}")


async def _send_period_push(period_name: str):
    async with AsyncSessionLocal() as db:
        try:
            await send_push_to_all(
                db,
                title="\U0001f4e2 Открыт прием показаний!",
                body=f"Начался расчётный период: {period_name}. Пожалуйста, передайте показания счётчиков в приложении."
            )
        except Exception as e:
            logger.error(f"Push notification error: {e}", exc_info=True)


@router.get("/api/admin/periods/close-preview", summary="Предпросмотр последствий закрытия периода")
async def close_period_preview(
        current_user: User = Depends(allow_period_management),
        db: AsyncSession = Depends(get_db)
):
    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()

    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода")

    pid = active_period.id

    (
        total_occupied_res,
        rooms_with_readings_res,
        drafts_res,
        anomalies_res,
        approved_stats_res,
        draft_sum_res,
    ) = await asyncio.gather(
        db.execute(
            select(func.count(func.distinct(User.room_id))).where(
                User.is_deleted.is_(False), User.room_id.is_not(None), User.role == "user"
            )
        ),
        db.execute(
            select(func.count(func.distinct(MeterReading.room_id))).where(
                MeterReading.period_id == pid
            )
        ),
        db.execute(
            select(func.count(MeterReading.id)).where(
                MeterReading.period_id == pid, MeterReading.is_approved.is_(False)
            )
        ),
        db.execute(
            select(func.count(MeterReading.id)).where(
                MeterReading.period_id == pid, MeterReading.is_approved.is_(False),
                MeterReading.anomaly_score >= 80
            )
        ),
        db.execute(
            select(
                func.count(MeterReading.id),
                func.coalesce(func.sum(MeterReading.total_cost), 0)
            ).where(
                MeterReading.period_id == pid, MeterReading.is_approved.is_(True)
            )
        ),
        db.execute(
            select(func.coalesce(func.sum(MeterReading.total_cost), 0)).where(
                MeterReading.period_id == pid, MeterReading.is_approved.is_(False)
            )
        ),
    )

    total_occupied_rooms = total_occupied_res.scalar_one()
    rooms_with_readings = rooms_with_readings_res.scalar_one()
    rooms_without_readings = max(0, total_occupied_rooms - rooms_with_readings)
    total_drafts = drafts_res.scalar_one()
    anomalies_count = anomalies_res.scalar_one()
    safe_drafts = max(0, total_drafts - anomalies_count)

    approved_row = approved_stats_res.one()
    approved_count = approved_row[0]
    approved_sum = float(approved_row[1])
    draft_sum = float(draft_sum_res.scalar_one())

    dorm_stats_res, dorm_submitted_res = await asyncio.gather(
        db.execute(
            select(Room.dormitory_name, func.count(func.distinct(User.room_id)).label("total_rooms"))
            .join(User, User.room_id == Room.id)
            .where(User.is_deleted.is_(False), User.role == "user")
            .group_by(Room.dormitory_name).order_by(Room.dormitory_name)
        ),
        db.execute(
            select(Room.dormitory_name, func.count(func.distinct(MeterReading.room_id)).label("submitted"))
            .join(MeterReading, MeterReading.room_id == Room.id)
            .where(MeterReading.period_id == pid)
            .group_by(Room.dormitory_name)
        ),
    )

    dorm_totals = {row[0]: row[1] for row in dorm_stats_res.all()}
    dorm_submitted = {row[0]: row[1] for row in dorm_submitted_res.all()}

    dormitories = []
    for dorm_name, total in sorted(dorm_totals.items()):
        submitted = dorm_submitted.get(dorm_name, 0)
        dormitories.append({
            "name": dorm_name, "total_rooms": total, "submitted": submitted,
            "missing": total - submitted,
            "percent": round(submitted / total * 100) if total > 0 else 0
        })

    return {
        "period_name": active_period.name,
        "total_occupied_rooms": total_occupied_rooms,
        "rooms_with_readings": rooms_with_readings,
        "rooms_without_readings": rooms_without_readings,
        "total_drafts": total_drafts, "anomalies_count": anomalies_count,
        "safe_drafts": safe_drafts, "approved_count": approved_count,
        "approved_sum": approved_sum, "draft_sum": draft_sum,
        "estimated_total": approved_sum + draft_sum, "dormitories": dormitories,
    }

@router.get("/api/admin/periods/compare", summary="Сравнение двух периодов по ресурсам")
async def compare_periods(
        period_a: int = Query(..., description="ID первого периода"),
        period_b: int = Query(..., description="ID второго периода"),
        current_user: User = Depends(allow_period_management),
        db: AsyncSession = Depends(get_db)
):
    pa = await db.get(BillingPeriod, period_a)
    pb = await db.get(BillingPeriod, period_b)
    if not pa or not pb:
        raise HTTPException(status_code=404, detail="Один или оба периода не найдены")

    cost_fields = [
        "cost_hot_water", "cost_cold_water", "cost_sewage",
        "cost_electricity", "cost_maintenance", "cost_social_rent",
        "cost_waste", "cost_fixed_part", "total_cost"
    ]

    async def _aggregate_period(pid: int):
        stmt = (
            select(
                Room.dormitory_name, func.count(MeterReading.id).label("records"),
                *[func.coalesce(func.sum(getattr(MeterReading, f)), 0).label(f) for f in cost_fields]
            )
            .join(Room, MeterReading.room_id == Room.id)
            .where(MeterReading.period_id == pid, MeterReading.is_approved.is_(True))
            .group_by(Room.dormitory_name)
        )
        result = await db.execute(stmt)
        data = {}
        for row in result.all():
            dorm = row[0] or "Без общежития"
            entry = {"records": row[1]}
            for i, field in enumerate(cost_fields):
                entry[field] = float(row[2 + i])
            data[dorm] = entry
        return data

    data_a, data_b = await asyncio.gather(
        _aggregate_period(period_a), _aggregate_period(period_b)
    )

    all_dorms = sorted(set(list(data_a.keys()) + list(data_b.keys())))

    def _safe_pct(old_val, new_val):
        if old_val == 0 and new_val == 0: return 0.0
        if old_val == 0: return 100.0
        return round((new_val - old_val) / abs(old_val) * 100, 1)

    empty_entry = {f: 0 for f in cost_fields}
    empty_entry["records"] = 0
    comparison = []

    # ИСПРАВЛЕНИЕ: Разделение команд на разные строки
    totals_a = {f: 0.0 for f in cost_fields}
    totals_a["records"] = 0
    totals_b = {f: 0.0 for f in cost_fields}
    totals_b["records"] = 0

    for dorm in all_dorms:
        a = data_a.get(dorm, empty_entry)
        b = data_b.get(dorm, empty_entry)
        deltas = {}
        for f in cost_fields:
            val_a = a.get(f, 0)
            val_b = b.get(f, 0)
            deltas[f] = {"period_a": val_a, "period_b": val_b, "delta": round(val_b - val_a, 2), "percent": _safe_pct(val_a, val_b)}
            totals_a[f] += val_a
            totals_b[f] += val_b
        totals_a["records"] += a.get("records", 0)
        totals_b["records"] += b.get("records", 0)
        comparison.append({"dormitory": dorm, "records_a": a.get("records", 0), "records_b": b.get("records", 0), "details": deltas})

    grand_deltas = {}
    for f in cost_fields:
        grand_deltas[f] = {
            "period_a": round(totals_a[f], 2), "period_b": round(totals_b[f], 2),
            "delta": round(totals_b[f] - totals_a[f], 2), "percent": _safe_pct(totals_a[f], totals_b[f])
        }

    return {
        "period_a": {"id": pa.id, "name": pa.name}, "period_b": {"id": pb.id, "name": pb.name},
        "dormitories": comparison,
        "totals": {"records_a": totals_a["records"], "records_b": totals_b["records"], "details": grand_deltas}
    }

@router.post("/api/admin/periods/open", summary="Открыть новый месяц",
             dependencies=[Depends(RateLimiter(times=1, seconds=10))])
async def api_open_period(data: PeriodCreate, background_tasks: BackgroundTasks,
                          current_user: User = Depends(allow_period_management), db: AsyncSession = Depends(get_db)):
    try:
        new_period = await open_new_period(db=db, new_name=data.name)
        await write_audit_log(
            db, current_user.id, current_user.username,
            action="open_period", entity_type="period", entity_id=new_period.id,
            details={"name": new_period.name}
        )
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        await db.rollback()
        logger.error(f"Critical error in api_open_period: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Внутренняя ошибка при открытии периода.")

    await _safe_clear_cache("periods")
    background_tasks.add_task(_send_period_push, new_period.name)
    return {"status": "opened", "period": new_period.name}

@router.post("/api/admin/periods/close", summary="Закрыть текущий месяц (Фоновая задача)",
             dependencies=[Depends(RateLimiter(times=1, seconds=30))])
async def api_close_period(current_user: User = Depends(allow_period_management), db: AsyncSession = Depends(get_db)):
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active.is_(True)))).scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода для закрытия")
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="close_period", entity_type="period", entity_id=active_period.id,
        details={"name": active_period.name, "triggered_by": current_user.username}
    )
    await db.commit()
    task = close_period_task.delay(current_user.id)
    return {"status": "processing", "task_id": task.id, "message": "Процесс закрытия периода запущен в фоне."}

@router.get("/api/admin/periods/active", response_model=Optional[PeriodResponse])
@cache(expire=300, namespace="periods")
async def get_active_period(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))
    return res.scalars().first()

@router.get("/api/admin/periods/history", response_model=List[PeriodResponse], summary="История всех периодов")
async def get_all_periods(current_user: User = Depends(allow_period_management), db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(BillingPeriod).order_by(desc(BillingPeriod.id)))
    return res.scalars().all()