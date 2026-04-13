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
        after_id: Optional[int],
        search: Optional[str],
        anomalies_only: bool,
        sort_by: str,
        sort_dir: str
):
    """Получение списка черновиков показаний для бухгалтера."""
    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active)
    )).scalars().first()

    if not active_period:
        return {"total": 0, "page": page, "size": limit, "items": []}

    query = (
        select(MeterReading, User, Room)
        .join(User, MeterReading.user_id == User.id)
        .join(Room, MeterReading.room_id == Room.id)
        .where(
            MeterReading.is_approved.is_(False),
            MeterReading.period_id == active_period.id
        )
    )

    # ИСПРАВЛЕНИЕ: был Python-оператор `is not None` который всегда True.
    # Правильный SQLAlchemy-метод: .isnot(None) — генерирует SQL: IS NOT NULL.
    if anomalies_only:
        query = query.where(MeterReading.anomaly_flags.isnot(None))

    if search:
        search_fmt = f"%{search}%"
        query = query.where(
            or_(
                User.username.ilike(search_fmt),
                Room.dormitory_name.ilike(search_fmt),
                Room.room_number.ilike(search_fmt)
            )
        )

    total = (await db.execute(
        select(func.count()).select_from(query.subquery())
    )).scalar_one()

    # ИСПРАВЛЕНИЕ: добавлен "created_at" в словарь маппинга.
    # Ранее дефолтное значение Query sort_by="created_at" не было в словаре
    # и тихо падало на сортировку по MeterReading.id вместо даты.
    sort_col = {
        "created_at": MeterReading.created_at,
        "id": MeterReading.id,
        "username": User.username,
        "dormitory": Room.dormitory_name,
        "total_cost": MeterReading.total_cost,
        "anomaly_score": MeterReading.anomaly_score,
    }.get(sort_by, MeterReading.created_at)  # default — по дате создания

    if after_id and sort_by in ["created_at", "id"]:
        if sort_dir == "desc":
            query = query.where(MeterReading.id < after_id).order_by(desc(MeterReading.id))
        else:
            query = query.where(MeterReading.id > after_id).order_by(asc(MeterReading.id))
        rows = (await db.execute(query.limit(limit))).all()
    else:
        query = query.order_by(asc(sort_col) if sort_dir == "asc" else desc(sort_col))
        rows = (await db.execute(query.offset((page - 1) * limit).limit(limit))).all()

    if not rows:
        return {"total": total, "page": page, "size": limit, "items": []}

    # Ищем предыдущие утверждённые показания по room_id для отображения разницы
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

    items = []

    for current, user, room in rows:
        prev = prev_map.get(room.id)
        anomaly_details = []

        if current.anomaly_flags and current.anomaly_flags != "PENDING":
            for flag_code in current.anomaly_flags.split(','):
                if not flag_code:
                    continue
                base_key = next(
                    (k for k in ANOMALY_MAP.keys() if flag_code.startswith(k)),
                    "UNKNOWN"
                )
                meta = ANOMALY_MAP.get(base_key, ANOMALY_MAP["UNKNOWN"])
                anomaly_details.append({
                    "code": flag_code,
                    "message": meta["message"],
                    "severity": meta["severity"],
                    "color": meta.get("color", "#9ca3af")
                })

        items.append({
            "id": current.id,
            "user_id": user.id,
            "username": user.username,
            "dormitory": f"{room.dormitory_name} ({room.room_number})",
            "prev_hot": prev.hot_water if prev else ZERO,
            "cur_hot": current.hot_water,
            "prev_cold": prev.cold_water if prev else ZERO,
            "cur_cold": current.cold_water,
            "prev_elect": prev.electricity if prev else ZERO,
            "cur_elect": current.electricity,
            "total_cost": current.total_cost,
            "residents_count": user.residents_count,
            "total_room_residents": room.total_room_residents,
            "created_at": current.created_at,
            "anomaly_flags": current.anomaly_flags,
            "anomaly_score": getattr(current, 'anomaly_score', 0),
            "anomaly_details": anomaly_details,
            "edit_count": current.edit_count or 0,
            "edit_history": current.edit_history or [],
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

    # Последнее утверждённое показание комнаты
    prev = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.room_id == user.room_id,
            MeterReading.is_approved.is_(True)
        )
        .order_by(desc(MeterReading.created_at))
        .limit(1)
    )).scalars().first()

    # Черновик текущего периода
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