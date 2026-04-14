# app/modules/utility/routers/admin_periods.py

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

router = APIRouter(tags=["Admin Periods"])
logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

allow_period_management = RoleChecker(["accountant", "admin"])


async def _send_period_push(period_name: str):
    """
    Фоновая задача отправки пуш-уведомлений при открытии периода.
    Использует свою собственную сессию БД — не зависит от сессии запроса.
    """
    async with AsyncSessionLocal() as db:
        try:
            await send_push_to_all(
                db,
                title="📢 Открыт прием показаний!",
                body=f"Начался расчётный период: {period_name}. Пожалуйста, передайте показания счётчиков в приложении."
            )
        except Exception as e:
            logger.error(f"Ошибка отправки пуш-уведомлений при открытии периода: {e}", exc_info=True)


# =====================================================================
# НОВАЯ ФУНКЦИЯ: ПРЕДПРОСМОТР ЗАКРЫТИЯ ПЕРИОДА
# =====================================================================
@router.get("/api/admin/periods/close-preview", summary="Предпросмотр последствий закрытия периода")
async def close_period_preview(
        current_user: User = Depends(allow_period_management),
        db: AsyncSession = Depends(get_db)
):
    """
    Показывает администратору что произойдёт при закрытии периода:
    - Сколько комнат передали показания
    - Сколько комнат НЕ передали (будут авто-сгенерированы)
    - Сколько аномалий ожидают проверки
    - Сколько черновиков будут утверждены автоматически
    - Предварительная итоговая сумма

    Администратор смотрит этот отчёт и принимает решение — закрывать или подождать.
    """
    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()

    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода")

    # 1. Все комнаты с жильцами
    total_occupied_rooms_result = await db.execute(
        select(func.count(func.distinct(User.room_id))).where(
            User.is_deleted.is_(False),
            User.room_id.is_not(None),
            User.role == "user"
        )
    )
    total_occupied_rooms = total_occupied_rooms_result.scalar_one()

    # 2. Комнаты, по которым есть показания в этом периоде
    rooms_with_readings_result = await db.execute(
        select(func.count(func.distinct(MeterReading.room_id))).where(
            MeterReading.period_id == active_period.id
        )
    )
    rooms_with_readings = rooms_with_readings_result.scalar_one()

    # 3. Комнаты без показаний (будут авто-сгенерированы)
    rooms_without_readings = max(0, total_occupied_rooms - rooms_with_readings)

    # 4. Черновики (неутверждённые показания)
    drafts_result = await db.execute(
        select(func.count(MeterReading.id)).where(
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(False)
        )
    )
    total_drafts = drafts_result.scalar_one()

    # 5. Аномалии (anomaly_score >= 80) — требуют ручной проверки
    anomalies_result = await db.execute(
        select(func.count(MeterReading.id)).where(
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(False),
            MeterReading.anomaly_score >= 80
        )
    )
    anomalies_count = anomalies_result.scalar_one()

    # 6. Безопасные черновики (будут утверждены автоматически)
    safe_drafts = max(0, total_drafts - anomalies_count)

    # 7. Уже утверждённые показания и их сумма
    approved_stats_result = await db.execute(
        select(
            func.count(MeterReading.id),
            func.coalesce(func.sum(MeterReading.total_cost), 0)
        ).where(
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(True)
        )
    )
    approved_row = approved_stats_result.one()
    approved_count = approved_row[0]
    approved_sum = float(approved_row[1])

    # 8. Сумма по неутверждённым черновикам (предварительная)
    draft_sum_result = await db.execute(
        select(func.coalesce(func.sum(MeterReading.total_cost), 0)).where(
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(False)
        )
    )
    draft_sum = float(draft_sum_result.scalar_one())

    # 9. Детализация по общежитиям: кто сдал, кто нет
    dorm_stats_result = await db.execute(
        select(
            Room.dormitory_name,
            func.count(func.distinct(User.room_id)).label("total_rooms"),
        )
        .join(User, User.room_id == Room.id)
        .where(User.is_deleted.is_(False), User.role == "user")
        .group_by(Room.dormitory_name)
        .order_by(Room.dormitory_name)
    )

    dorm_submitted_result = await db.execute(
        select(
            Room.dormitory_name,
            func.count(func.distinct(MeterReading.room_id)).label("submitted"),
        )
        .join(MeterReading, MeterReading.room_id == Room.id)
        .where(MeterReading.period_id == active_period.id)
        .group_by(Room.dormitory_name)
    )

    dorm_totals = {row[0]: row[1] for row in dorm_stats_result.all()}
    dorm_submitted = {row[0]: row[1] for row in dorm_submitted_result.all()}

    dormitories = []
    for dorm_name, total in sorted(dorm_totals.items()):
        submitted = dorm_submitted.get(dorm_name, 0)
        dormitories.append({
            "name": dorm_name,
            "total_rooms": total,
            "submitted": submitted,
            "missing": total - submitted,
            "percent": round(submitted / total * 100) if total > 0 else 0
        })

    return {
        "period_name": active_period.name,
        "total_occupied_rooms": total_occupied_rooms,
        "rooms_with_readings": rooms_with_readings,
        "rooms_without_readings": rooms_without_readings,
        "total_drafts": total_drafts,
        "anomalies_count": anomalies_count,
        "safe_drafts": safe_drafts,
        "approved_count": approved_count,
        "approved_sum": approved_sum,
        "draft_sum": draft_sum,
        "estimated_total": approved_sum + draft_sum,
        "dormitories": dormitories,
    }


# =====================================================================
# НОВАЯ ФУНКЦИЯ: СРАВНИТЕЛЬНАЯ АНАЛИТИКА ПЕРИОДОВ
# =====================================================================
@router.get("/api/admin/periods/compare", summary="Сравнение двух периодов по ресурсам")
async def compare_periods(
        period_a: int = Query(..., description="ID первого периода"),
        period_b: int = Query(..., description="ID второго периода"),
        current_user: User = Depends(allow_period_management),
        db: AsyncSession = Depends(get_db)
):
    """
    Сравнивает два расчётных периода по каждому ресурсу и общежитию.
    Показывает дельту (разницу) и процент изменения.

    Пример: администратор выбирает «Январь 2026» и «Февраль 2026» —
    видит что в общежитии №3 электричество выросло на 25%, а вода упала на 5%.
    Это позволяет ловить утечки, сезонные отклонения и ошибки учёта.
    """
    # Проверяем что оба периода существуют
    pa = await db.get(BillingPeriod, period_a)
    pb = await db.get(BillingPeriod, period_b)
    if not pa or not pb:
        raise HTTPException(status_code=404, detail="Один или оба периода не найдены")

    # Ключевые поля для сравнения
    cost_fields = [
        "cost_hot_water", "cost_cold_water", "cost_sewage",
        "cost_electricity", "cost_maintenance", "cost_social_rent",
        "cost_waste", "cost_fixed_part", "total_cost"
    ]

    async def _aggregate_period(pid: int):
        """Агрегация по одному периоду, сгруппированная по общежитию."""
        stmt = (
            select(
                Room.dormitory_name,
                func.count(MeterReading.id).label("records"),
                *[func.coalesce(func.sum(getattr(MeterReading, f)), 0).label(f) for f in cost_fields]
            )
            .join(Room, MeterReading.room_id == Room.id)
            .where(
                MeterReading.period_id == pid,
                MeterReading.is_approved.is_(True)
            )
            .group_by(Room.dormitory_name)
        )
        result = await db.execute(stmt)
        rows = result.all()

        data = {}
        for row in rows:
            dorm = row[0] or "Без общежития"
            entry = {"records": row[1]}
            for i, field in enumerate(cost_fields):
                entry[field] = float(row[2 + i])
            data[dorm] = entry
        return data

    data_a = await _aggregate_period(period_a)
    data_b = await _aggregate_period(period_b)

    # Объединяем ключи
    all_dorms = sorted(set(list(data_a.keys()) + list(data_b.keys())))

    def _safe_pct(old_val, new_val):
        """Безопасный расчёт процента изменения."""
        if old_val == 0 and new_val == 0:
            return 0.0
        if old_val == 0:
            return 100.0
        return round((new_val - old_val) / abs(old_val) * 100, 1)

    empty_entry = {f: 0 for f in cost_fields}
    empty_entry["records"] = 0

    comparison = []
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
            deltas[f] = {
                "period_a": val_a,
                "period_b": val_b,
                "delta": round(val_b - val_a, 2),
                "percent": _safe_pct(val_a, val_b)
            }
            totals_a[f] += val_a
            totals_b[f] += val_b

        totals_a["records"] += a.get("records", 0)
        totals_b["records"] += b.get("records", 0)

        comparison.append({
            "dormitory": dorm,
            "records_a": a.get("records", 0),
            "records_b": b.get("records", 0),
            "details": deltas
        })

    # Итоговая строка
    grand_deltas = {}
    for f in cost_fields:
        grand_deltas[f] = {
            "period_a": round(totals_a[f], 2),
            "period_b": round(totals_b[f], 2),
            "delta": round(totals_b[f] - totals_a[f], 2),
            "percent": _safe_pct(totals_a[f], totals_b[f])
        }

    return {
        "period_a": {"id": pa.id, "name": pa.name},
        "period_b": {"id": pb.id, "name": pb.name},
        "dormitories": comparison,
        "totals": {
            "records_a": totals_a["records"],
            "records_b": totals_b["records"],
            "details": grand_deltas
        }
    }


# =====================================================================
# СУЩЕСТВУЮЩИЕ ENDPOINTS (без изменений, с исправлениями из группы A)
# =====================================================================

@router.post(
    "/api/admin/periods/close",
    summary="Закрыть текущий месяц (Фоновая задача)",
    dependencies=[Depends(RateLimiter(times=1, seconds=30))]
)
async def api_close_period(
        current_user: User = Depends(allow_period_management),
        db: AsyncSession = Depends(get_db)
):
    active_check = await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )
    if not active_check.scalars().first():
        raise HTTPException(status_code=400, detail="Нет активного периода для закрытия")

    task = close_period_task.delay(current_user.id)

    return {
        "status": "processing",
        "task_id": task.id,
        "message": "Процесс закрытия периода запущен в фоне."
    }


@router.post(
    "/api/admin/periods/open",
    summary="Открыть новый месяц",
    dependencies=[Depends(RateLimiter(times=1, seconds=10))]
)
async def api_open_period(
        data: PeriodCreate,
        background_tasks: BackgroundTasks,
        current_user: User = Depends(allow_period_management),
        db: AsyncSession = Depends(get_db)
):
    try:
        new_period = await open_new_period(db=db, new_name=data.name)
        await db.commit()
        await FastAPICache.clear(namespace="periods")

        background_tasks.add_task(_send_period_push, new_period.name)

        return {"status": "opened", "period": new_period.name}

    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        await db.rollback()
        logger.error(f"Critical error in api_open_period: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при открытии периода. Обратитесь к администратору."
        )


@router.get("/api/admin/periods/active", response_model=Optional[PeriodResponse])
@cache(expire=300, namespace="periods")
async def get_active_period(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))
    return res.scalars().first()


@router.get("/api/admin/periods/history", response_model=List[PeriodResponse], summary="История всех периодов")
async def get_all_periods(
        current_user: User = Depends(allow_period_management),
        db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(BillingPeriod).order_by(desc(BillingPeriod.id)))
    return res.scalars().all()