# app/modules/utility/services/admin_readings_list.py

import logging
from decimal import Decimal
from typing import Optional
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, asc, func, or_
from sqlalchemy.orm import selectinload

from app.modules.utility.models import User, MeterReading, BillingPeriod, Room
from app.modules.utility.constants import ANOMALY_MAP

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


async def get_paginated_readings(
        db: AsyncSession,
        page: int,
        limit: int,
        cursor_id: Optional[int],
        direction: str,
        search: Optional[str],
        anomalies_only: bool,
        sort_by: str,
        sort_dir: str
):
    """
    Получение списка черновиков показаний для бухгалтера.

    ИСПРАВЛЕНИЕ P2: COUNT запрос оптимизирован.
    Ранее: SELECT COUNT(*) FROM (SELECT ... JOIN User JOIN Room ... WHERE ...) — материализация
    всего отфильтрованного набора с JOINами как подзапрос. На партицированной таблице readings
    с миллионами строк это sequential scan.

    Теперь: Для базового случая (без поиска) COUNT идёт напрямую по readings без JOINов.
    При поиске — лёгкий COUNT с минимальными JOINами без ORDER BY и LIMIT.
    """
    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active)
    )).scalars().first()

    if not active_period:
        return {"total": 0, "page": page, "size": limit, "items": []}

    # =====================================================
    # COUNT — отдельный лёгкий запрос
    # =====================================================
    base_filter = [
        MeterReading.is_approved.is_(False),
        MeterReading.period_id == active_period.id
    ]

    if anomalies_only:
        base_filter.append(MeterReading.anomaly_flags.isnot(None))

    if search:
        # При поиске нужен JOIN, но без ORDER BY и без выборки всех колонок
        search_fmt = f"%{search}%"
        count_query = (
            select(func.count(MeterReading.id))
            .join(User, MeterReading.user_id == User.id)
            .join(Room, MeterReading.room_id == Room.id)
            .where(
                *base_filter,
                or_(
                    User.username.ilike(search_fmt),
                    Room.dormitory_name.ilike(search_fmt),
                    Room.room_number.ilike(search_fmt)
                )
            )
        )
    else:
        # Без поиска: COUNT напрямую по readings, без JOIN — максимально быстро
        count_query = select(func.count(MeterReading.id)).where(*base_filter)

    total = (await db.execute(count_query)).scalar_one()

    # =====================================================
    # DATA — основной запрос с JOINами
    # =====================================================
    query = (
        select(MeterReading, User, Room)
        .join(User, MeterReading.user_id == User.id)
        .join(Room, MeterReading.room_id == Room.id)
        .where(*base_filter)
    )

    if search:
        search_fmt = f"%{search}%"
        query = query.where(
            or_(
                User.username.ilike(search_fmt),
                Room.dormitory_name.ilike(search_fmt),
                Room.room_number.ilike(search_fmt)
            )
        )

    # Транслируем created_at в id для Keyset Pagination
    if sort_by == "created_at":
        sort_by = "id"

    sort_col_map = {
        "id": MeterReading.id,
        "username": User.username,
        "dormitory": Room.dormitory_name,
        "total_cost": MeterReading.total_cost,
        "anomaly_score": MeterReading.anomaly_score,
    }
    sort_col = sort_col_map.get(sort_by, MeterReading.id)

    use_keyset = (sort_by == "id")

    # === ЛОГИКА ПАГИНАЦИИ ===
    if use_keyset and cursor_id is not None:
        if direction == "next":
            if sort_dir == "desc":
                query = query.where(MeterReading.id < cursor_id)
            else:
                query = query.where(MeterReading.id > cursor_id)
        else:  # prev
            if sort_dir == "desc":
                query = query.where(MeterReading.id > cursor_id)
            else:
                query = query.where(MeterReading.id < cursor_id)
    else:
        # Fallback для поиска или сложной сортировки (OFFSET)
        query = query.offset((page - 1) * limit)

    # === ЛОГИКА СОРТИРОВКИ ===
    if use_keyset and direction == "prev":
        query = query.order_by(asc(sort_col) if sort_dir == "desc" else desc(sort_col))
    else:
        query = query.order_by(desc(sort_col) if sort_dir == "desc" else asc(sort_col))

    query = query.limit(limit)
    rows = (await db.execute(query)).all()

    if use_keyset and direction == "prev":
        rows.reverse()

    if not rows:
        return {"total": total, "page": page, "size": limit, "items": []}

    # =====================================================
    # Предыдущие показания — batch-запрос
    # =====================================================
    room_ids = [row[2].id for row in rows]
    subq_max_prev = select(
        MeterReading.room_id,
        func.max(MeterReading.created_at).label("max_created")
    ).where(
        MeterReading.room_id.in_(room_ids),
        MeterReading.is_approved.is_(True)
    ).group_by(MeterReading.room_id).subquery()

    stmt_prev = select(MeterReading).join(
        subq_max_prev,
        (MeterReading.room_id == subq_max_prev.c.room_id) &
        (MeterReading.created_at == subq_max_prev.c.max_created)
    )
    prev_map = {r.room_id: r for r in (await db.execute(stmt_prev)).scalars().all()}

    # =====================================================
    # Сборка ответа
    # =====================================================
    items = []
    for current, user, room in rows:
        prev = prev_map.get(room.id)
        anomaly_details = []

        if current.anomaly_flags and current.anomaly_flags != "PENDING":
            for flag in current.anomaly_flags.split(","):
                flag = flag.strip()
                if flag:
                    label = ANOMALY_MAP.get(flag, flag)
                    anomaly_details.append({"code": flag, "label": label})

        d_hot = (current.hot_water or ZERO) - (prev.hot_water if prev else ZERO)
        d_cold = (current.cold_water or ZERO) - (prev.cold_water if prev else ZERO)
        d_elect = (current.electricity or ZERO) - (prev.electricity if prev else ZERO)

        items.append({
            "id": current.id,
            "user_id": user.id,
            "username": user.username,
            "dormitory": room.dormitory_name,
            "room_number": room.room_number,
            "hot_water": current.hot_water,
            "cold_water": current.cold_water,
            "electricity": current.electricity,
            "delta_hot": d_hot,
            "delta_cold": d_cold,
            "delta_elect": d_elect,
            "total_cost": current.total_cost,
            "total_209": current.total_209,
            "total_205": current.total_205,
            "anomaly_score": current.anomaly_score,
            "anomaly_flags": current.anomaly_flags,
            "anomaly_details": anomaly_details,
            "created_at": current.created_at.isoformat() if current.created_at else None,
        })

    return {"total": total, "page": page, "size": limit, "items": items}

async def get_manual_state(db: AsyncSession, user_id: int):
    """Получение состояния для формы ручного ввода показаний."""
    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(
            User.id == user_id,
            User.is_deleted.is_(False)
        )
    )).scalars().first()

    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if not user.room:
        return {
            "user_id": user.id,
            "username": user.username,
            "room": None,
            "prev_hot": ZERO,
            "prev_cold": ZERO,
            "prev_elect": ZERO,
            "has_draft": False,
        }

    prev = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.room_id == user.room_id,
            MeterReading.is_approved.is_(True)
        )
        .order_by(desc(MeterReading.created_at))
        .limit(1)
    )).scalars().first()

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active)
    )).scalars().first()

    draft = None
    if active_period:
        draft = (await db.execute(
            select(MeterReading).where(
                MeterReading.room_id == user.room_id,
                MeterReading.period_id == active_period.id,
                MeterReading.is_approved.is_(False)
            )
        )).scalars().first()

    return {
        "user_id": user.id,
        "username": user.username,
        "room": {
            "id": user.room.id,
            "dormitory_name": user.room.dormitory_name,
            "room_number": user.room.room_number,
        },
        "prev_hot": prev.hot_water if prev else ZERO,
        "prev_cold": prev.cold_water if prev else ZERO,
        "prev_elect": prev.electricity if prev else ZERO,
        "has_draft": draft is not None,
        "draft_hot": draft.hot_water if draft else None,
        "draft_cold": draft.cold_water if draft else None,
        "draft_elect": draft.electricity if draft else None,
    }