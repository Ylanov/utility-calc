# app/modules/utility/routers/admin_dashboard.py

import logging
from typing import Optional
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, desc, case

from app.core.database import get_db
from app.modules.utility.models import (
    User, Room, MeterReading, BillingPeriod, Adjustment, AuditLog
)
from app.core.dependencies import get_current_user, RoleChecker

router = APIRouter(tags=["Admin Dashboard"])
logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
allow_dashboard = RoleChecker(["accountant", "admin", "financier"])


# =====================================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: Запись в журнал действий
# =====================================================================
async def write_audit_log(
    db: AsyncSession,
    user_id: int,
    username: str,
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    details: Optional[dict] = None
):
    """
    Универсальная функция записи действия в журнал.
    Импортируется и используется в любом роутере:

        from app.modules.utility.routers.admin_dashboard import write_audit_log
        await write_audit_log(db, user.id, user.username, "approve", "reading", reading.id, {"total": 1234})
    """
    try:
        log_entry = AuditLog(
            user_id=user_id,
            username=username,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details
        )
        db.add(log_entry)
        # НЕ делаем commit — он произойдёт в вызывающей транзакции.
        # Это гарантирует что запись в лог атомарна с основным действием.
    except Exception as e:
        # Логирование не должно ломать основную операцию
        logger.error(f"Failed to write audit log: {e}")


# =====================================================================
# ДАШБОРД: КЛЮЧЕВЫЕ ПОКАЗАТЕЛИ (KPI)
# =====================================================================
@router.get("/api/admin/dashboard", summary="KPI дашборд администратора")
async def get_dashboard_kpi(
    current_user: User = Depends(allow_dashboard),
    db: AsyncSession = Depends(get_db)
):
    """
    Возвращает ключевые метрики для главного экрана администратора:
    - Жильцы: всего активных, с долгами
    - Комнаты: занятые, свободные
    - Текущий период: % сдавших, черновики, аномалии
    - Финансы: общая сумма начислений, задолженность
    - Сравнение с предыдущим периодом
    """

    # === ЖИЛЬЦЫ ===
    users_stats = await db.execute(
        select(
            func.count(User.id).label("total"),
            func.count(case((User.room_id.is_not(None), 1))).label("with_room"),
        ).where(User.is_deleted.is_(False), User.role == "user")
    )
    u = users_stats.one()
    total_users = u[0]
    users_with_room = u[1]

    # === КОМНАТЫ ===
    rooms_stats = await db.execute(
        select(func.count(Room.id))
    )
    total_rooms = rooms_stats.scalar_one()

    occupied_rooms_result = await db.execute(
        select(func.count(func.distinct(User.room_id))).where(
            User.is_deleted.is_(False), User.room_id.is_not(None), User.role == "user"
        )
    )
    occupied_rooms = occupied_rooms_result.scalar_one()

    # === АКТИВНЫЙ ПЕРИОД ===
    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()

    period_data = None
    finance_data = None
    comparison = None

    if active_period:
        # Комнаты с показаниями
        submitted = (await db.execute(
            select(func.count(func.distinct(MeterReading.room_id))).where(
                MeterReading.period_id == active_period.id
            )
        )).scalar_one()

        # Черновики и аномалии
        drafts_stats = await db.execute(
            select(
                func.count(MeterReading.id).label("total_drafts"),
                func.count(case((MeterReading.anomaly_score >= 80, 1))).label("anomalies"),
            ).where(
                MeterReading.period_id == active_period.id,
                MeterReading.is_approved.is_(False)
            )
        )
        ds = drafts_stats.one()

        # Уже утверждённые
        approved_stats = await db.execute(
            select(
                func.count(MeterReading.id),
                func.coalesce(func.sum(MeterReading.total_cost), 0)
            ).where(
                MeterReading.period_id == active_period.id,
                MeterReading.is_approved.is_(True)
            )
        )
        ap = approved_stats.one()

        pct = round(submitted / occupied_rooms * 100) if occupied_rooms > 0 else 0

        period_data = {
            "name": active_period.name,
            "submitted_rooms": submitted,
            "total_occupied_rooms": occupied_rooms,
            "submit_percent": pct,
            "total_drafts": ds[0],
            "anomalies": ds[1],
            "approved_count": ap[0],
            "approved_sum": float(ap[1]),
        }

        # === СРАВНЕНИЕ С ПРЕДЫДУЩИМ ПЕРИОДОМ ===
        prev_period = (await db.execute(
            select(BillingPeriod)
            .where(BillingPeriod.id < active_period.id)
            .order_by(desc(BillingPeriod.id))
            .limit(1)
        )).scalars().first()

        if prev_period:
            prev_sum_result = await db.execute(
                select(func.coalesce(func.sum(MeterReading.total_cost), 0)).where(
                    MeterReading.period_id == prev_period.id,
                    MeterReading.is_approved.is_(True)
                )
            )
            prev_sum = float(prev_sum_result.scalar_one())

            current_sum = float(ap[1])
            delta = current_sum - prev_sum
            pct_change = round((delta / prev_sum * 100), 1) if prev_sum > 0 else 0

            comparison = {
                "prev_period_name": prev_period.name,
                "prev_sum": prev_sum,
                "current_sum": current_sum,
                "delta": delta,
                "percent_change": pct_change,
            }

    # === ОБЩАЯ ЗАДОЛЖЕННОСТЬ (по последним показаниям каждого жильца) ===
    debt_result = await db.execute(
        select(
            func.coalesce(func.sum(MeterReading.debt_209), 0),
            func.coalesce(func.sum(MeterReading.debt_205), 0),
        ).where(MeterReading.is_approved.is_(True))
    )
    debt_row = debt_result.one()
    total_debt = float(debt_row[0]) + float(debt_row[1])

    return {
        "users": {
            "total": total_users,
            "with_room": users_with_room,
            "without_room": total_users - users_with_room,
        },
        "rooms": {
            "total": total_rooms,
            "occupied": occupied_rooms,
            "empty": total_rooms - occupied_rooms,
        },
        "period": period_data,
        "comparison": comparison,
        "total_debt": total_debt,
    }


# =====================================================================
# ЖУРНАЛ ДЕЙСТВИЙ (AUDIT LOG)
# =====================================================================
@router.get("/api/admin/audit-log", summary="Журнал действий администратора")
async def get_audit_log(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=200),
    action: Optional[str] = Query(None, description="Фильтр по типу действия"),
    entity_type: Optional[str] = Query(None, description="Фильтр по типу сущности"),
    user_id: Optional[int] = Query(None, description="Фильтр по пользователю"),
    current_user: User = Depends(allow_dashboard),
    db: AsyncSession = Depends(get_db)
):
    """Постраничный журнал действий с фильтрацией."""
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))

    if action:
        query = query.where(AuditLog.action == action)
        count_query = count_query.where(AuditLog.action == action)

    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
        count_query = count_query.where(AuditLog.entity_type == entity_type)

    if user_id:
        query = query.where(AuditLog.user_id == user_id)
        count_query = count_query.where(AuditLog.user_id == user_id)

    total = (await db.execute(count_query)).scalar_one()

    rows = (await db.execute(
        query.order_by(desc(AuditLog.created_at))
        .offset((page - 1) * limit)
        .limit(limit)
    )).scalars().all()

    items = []
    for log in rows:
        items.append({
            "id": log.id,
            "username": log.username,
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "details": log.details,
            "created_at": log.created_at.strftime("%d.%m.%Y %H:%M") if log.created_at else None,
        })

    return {"total": total, "page": page, "size": limit, "items": items}


@router.get("/api/admin/audit-log/actions", summary="Список типов действий для фильтра")
async def get_audit_actions(
    current_user: User = Depends(allow_dashboard),
    db: AsyncSession = Depends(get_db)
):
    """Возвращает уникальные типы действий и сущностей для фильтров."""
    actions_result = await db.execute(
        select(AuditLog.action, func.count(AuditLog.id).label("cnt"))
        .group_by(AuditLog.action)
        .order_by(desc("cnt"))
    )
    entities_result = await db.execute(
        select(AuditLog.entity_type, func.count(AuditLog.id).label("cnt"))
        .group_by(AuditLog.entity_type)
        .order_by(desc("cnt"))
    )
    return {
        "actions": [{"name": r[0], "count": r[1]} for r in actions_result.all()],
        "entities": [{"name": r[0], "count": r[1]} for r in entities_result.all()],
    }