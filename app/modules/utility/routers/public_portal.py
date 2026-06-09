"""Анонимный QR-портал по квартире (без ФИО/адреса).

/api/q/{token}/state  — что показать на портале (окно подачи, нужны ли счётчики,
                        текущий черновик для «исправить», статус квитанции).
/api/q/{token}/submit — подача показаний; резолв комнаты по токену →
                        «представитель комнаты» → ОБЩИЙ сервис perform_reading_submission
                        (тот же биллинг-путь, что и резидентская ручка).

Авторизация = сам токен (неугадываемый, QR внутри квартиры у счётчика).
Никаких ФИО/адресов в ответах. Фаза 2 добавит сюда receipt + contact.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.utility.models import (
    BillingPeriod, MeterReading, Tariff, User,
)
from app.modules.utility.schemas import ReadingSchema
from app.modules.utility.routers.client_readings import (
    perform_reading_submission, _is_submission_day_open,
)
from app.modules.utility.services.qr_portal import (
    resolve_room_by_token, pick_representative_user_id,
)

router = APIRouter(prefix="/api/q", tags=["QR Portal (public)"])
logger = logging.getLogger(__name__)


async def _resolve_or_404(db: AsyncSession, token: str):
    room = await resolve_room_by_token(db, token)
    if not room:
        # Намеренно глухой 404 — не раскрываем, существует токен или нет.
        raise HTTPException(status_code=404, detail="Код не найден или больше не действует.")
    return room


def _is_house(room) -> bool:
    pt = getattr(room.place_type, "value", room.place_type)
    return pt == "house"


async def _active_period(db: AsyncSession):
    return (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active)
    )).scalars().first()


@router.get("/{token}/state")
async def portal_state(token: str, db: AsyncSession = Depends(get_db)):
    """Состояние портала для квартиры. Без ФИО/адреса."""
    room = await _resolve_or_404(db, token)
    period = await _active_period(db)
    rep_id = await pick_representative_user_id(db, room.id, period.id if period else None)

    # Нужно ли вообще подавать счётчики (дом / койко-место / тариф без счётчиков)?
    metered = not _is_house(room)
    rep = None
    if rep_id:
        rep = (await db.execute(select(User).where(User.id == rep_id))).scalars().first()
        if rep and getattr(rep, "billing_mode", "by_meter") == "per_capita":
            metered = False
        if metered and rep:
            from app.modules.utility.services.tariff_cache import tariff_cache
            t = tariff_cache.get_effective_tariff(user=rep, room=room)
            if t and not any([
                bool(getattr(t, "charge_hot_water", True)),
                bool(getattr(t, "charge_cold_water", True)),
                bool(getattr(t, "charge_electricity", True)),
            ]):
                metered = False

    day_open, today_day, start_day, end_day = await _is_submission_day_open(db)

    # Текущий черновик периода (для предзаполнения формы «исправить») и
    # утверждённое показание (квитанция готова, форма заблокирована).
    draft = approved = None
    if period:
        draft = (await db.execute(
            select(MeterReading).where(
                MeterReading.room_id == room.id,
                MeterReading.period_id == period.id,
                MeterReading.is_approved.is_(False),
            )
        )).scalars().first()
        approved = (await db.execute(
            select(MeterReading).where(
                MeterReading.room_id == room.id,
                MeterReading.period_id == period.id,
                MeterReading.is_approved.is_(True),
            )
        )).scalars().first()

    cur = None
    src = draft or approved
    if src and metered:
        cur = {
            "hot_water": str(src.hot_water) if src.hot_water is not None else "",
            "cold_water": str(src.cold_water) if src.cold_water is not None else "",
            "electricity": str(src.electricity) if src.electricity is not None else "",
        }

    return {
        "period": period.name if period else None,
        "has_period": bool(period),
        "metered": metered,
        "no_residents": rep_id is None,
        "window_open": day_open,
        "window": {"start": start_day, "end": end_day, "today": today_day},
        "submitted": bool(draft or approved),
        "approved": bool(approved),         # утверждено → правка закрыта, квитанция готова
        "editable": bool(draft) and not bool(approved),
        "current": cur,
    }


@router.post("/{token}/submit")
async def portal_submit(token: str, data: ReadingSchema, db: AsyncSession = Depends(get_db)):
    """Подача/правка показаний по QR-токену. Вся логика — общий сервис."""
    room = await _resolve_or_404(db, token)
    period = await _active_period(db)
    rep_id = await pick_representative_user_id(db, room.id, period.id if period else None)
    if not rep_id:
        raise HTTPException(
            status_code=400,
            detail="В этой квартире нет зарегистрированных жильцов. Обратитесь к администратору.",
        )
    # perform_reading_submission сам проверит дом/койко-место/окно/формат 5+3
    # и вернёт понятную 400-ошибку — пробрасываем как есть.
    result = await perform_reading_submission(db, rep_id, data)
    logger.info("[QR-PORTAL] подача room=%s rep_user=%s", room.id, rep_id)
    return result
