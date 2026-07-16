"""«Осиротевшие» показания: подачи жильца в ЧУЖОЙ (прежней) комнате.

Инвариант (инцидент Безродний 2026-07-16): показания жильца должны жить в
его ТЕКУЩЕЙ квартире. Показания привязаны к комнате (MeterReading.room_id)
— это правильно и не меняется; но когда админ ИСПРАВЛЯЕТ привязку жильца
(та же «к. 403», соседнее здание), уже поданные показания остаются в
старой комнате → реестр не находит prev по паре (user, room), дельты
пустые, месяц пересчитывается как baseline с нулевым расходом.

Отличить исправление привязки от ФИЗИЧЕСКОГО переезда автоматически нельзя
(при переезде показания старой комнаты должны остаться в ней — по ним
закрывается месяц старой квартиры), поэтому перенос — только по
подтверждению админа в диалоге.

Правила (ужесточены ревью 2026-07-16, 5 находок):
  - Переносится ВСЯ история подач жильца из чужих комнат (period_id NOT
    NULL), не только активный месяц: перенос одного месяца без истории
    воспроизводил бы тот же баг (recalc не находит prev в новой комнате
    → нули). Baseline-строки комнат (period_id IS NULL) не трогаем —
    они принадлежат КОМНАТЕ.
  - Сальдо-заглушки 1С (source='saldo' / все счётчики NULL) исключены
    всюду: это носители долга, не подачи.
  - Конфликт по месяцу (в текущей комнате уже есть показание этого
    периода) → строка пропускается с причиной, админ решает в реестре.
  - После переноса пересчитываются ВСЕ перенесённые месяцы (идемпотентно);
    ошибки пересчёта логируются и возвращаются, не глотаются.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.utility.models import BillingPeriod, MeterReading, Room, User
from app.modules.utility.services.period_helpers import period_chron_key

logger = logging.getLogger(__name__)


def _not_saldo():
    """Фильтр «настоящая подача»: не сальдо-заглушка 1С."""
    return or_(
        MeterReading.source.is_(None),
        MeterReading.source != "saldo",
    ), or_(
        MeterReading.hot_water.isnot(None),
        MeterReading.cold_water.isnot(None),
        MeterReading.electricity.isnot(None),
    )


async def _active_period(db: AsyncSession) -> BillingPeriod | None:
    return (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()


async def find_stranded(db: AsyncSession, user_id: int) -> dict:
    """Подачи жильца (все месяцы) не в его текущей комнате — для диалога."""
    user = await db.get(User, user_id)
    if not user or user.is_deleted or not user.room_id:
        return {"items": []}
    rows = (await db.execute(
        select(MeterReading, Room, BillingPeriod.name)
        .join(Room, Room.id == MeterReading.room_id)
        .join(BillingPeriod, BillingPeriod.id == MeterReading.period_id)
        .where(
            MeterReading.user_id == user_id,
            MeterReading.period_id.isnot(None),
            MeterReading.room_id != user.room_id,
            *_not_saldo(),
        )
    )).all()
    return {"items": [{
        "reading_id": r.id,
        "room_id": room.id,
        "room_label": room.format_address,
        "period_name": pname,
        "is_approved": bool(r.is_approved),
    } for r, room, pname in rows]}


async def count_stranded_global(db: AsyncSession) -> int:
    """Жильцы с подачей АКТИВНОГО месяца в чужой комнате, у которых в
    ТЕКУЩЕЙ комнате показания за месяц нет (симптом сломанных дельт).
    Страховка system_health. Если показание в текущей комнате уже есть —
    это дубль месяца, его видно в реестре, здесь не считаем."""
    active = await _active_period(db)
    if not active:
        return 0
    in_current = (
        select(MeterReading.id)
        .where(
            MeterReading.user_id == User.id,
            MeterReading.period_id == active.id,
            MeterReading.room_id == User.room_id,
        ).limit(1)
    ).exists()
    return int((await db.execute(
        select(func.count(func.distinct(MeterReading.user_id)))
        .join(User, User.id == MeterReading.user_id)
        .where(
            MeterReading.period_id == active.id,
            MeterReading.room_id.isnot(None),
            User.room_id.isnot(None),
            MeterReading.room_id != User.room_id,
            User.is_deleted.is_(False),
            *_not_saldo(),
            ~in_current,
        )
    )).scalar() or 0)


async def adopt_for_user(db: AsyncSession, user: User, actor: User | None) -> dict:
    """Перенос ВСЕЙ истории подач жильца из чужих комнат в текущую +
    пересчёт перенесённых месяцев. Только ПО ПОДТВЕРЖДЕНИЮ админа (при
    физическом переезде показания должны остаться в старой комнате —
    подтверждение как раз это отличает). Коммитит сам."""
    if not user.room_id:
        return {"moved": 0, "skipped": 0, "recalced": 0, "recalc_errors": 0}

    rows = (await db.execute(
        select(MeterReading, BillingPeriod.name)
        .join(BillingPeriod, BillingPeriod.id == MeterReading.period_id)
        .where(
            MeterReading.user_id == user.id,
            MeterReading.period_id.isnot(None),
            MeterReading.room_id.isnot(None),
            MeterReading.room_id != user.room_id,
            *_not_saldo(),
        )
    )).all()
    if not rows:
        return {"moved": 0, "skipped": 0, "recalced": 0, "recalc_errors": 0}

    # Занятые месяцы текущей комнаты — конфликтные не переносим (дубль).
    occupied = {pid for (pid,) in (await db.execute(
        select(MeterReading.period_id).where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == user.room_id,
            MeterReading.period_id.isnot(None),
        )
    )).all()}

    # От старых месяцев к новым; при дубле периода среди stranded —
    # приоритет утверждённому (черновик пропускается как конфликт).
    rows.sort(key=lambda t: (period_chron_key(t[1]),
                             0 if t[0].is_approved else 1))

    moved, skipped = [], []
    for r, pname in rows:
        if r.period_id in occupied:
            skipped.append({"reading_id": r.id, "period_name": pname,
                            "reason": "в текущей квартире уже есть показание этого месяца"})
            continue
        r.room_id = user.room_id
        occupied.add(r.period_id)
        moved.append((r.id, r.period_id, pname))

    if actor is not None and moved:
        from app.modules.utility.routers.admin_dashboard import write_audit_log
        await write_audit_log(
            db, actor.id, actor.username,
            action="adopt_stranded_readings", entity_type="user", entity_id=user.id,
            details={"to_room_id": user.room_id,
                     "moved": [{"reading_id": i, "period_id": p} for i, p, _ in moved],
                     "skipped": skipped},
        )
    await db.commit()

    # Пересчёт всех перенесённых месяцев (от старых к новым — prev цепочкой).
    recalced, recalc_errors = 0, 0
    if moved:
        from app.modules.utility.services.admin_readings_manual import recalc_user_period
        for _rid, pid, pname in moved:
            try:
                await recalc_user_period(db, user_id=user.id, period_id=pid)
                recalced += 1
            except Exception:
                recalc_errors += 1
                logger.exception(
                    "[adopt_stranded] recalc failed user=%s period=%s", user.id, pid)

    return {"moved": len(moved), "skipped": len(skipped),
            "skipped_detail": skipped, "recalced": recalced,
            "recalc_errors": recalc_errors,
            "moved_periods": [pname for _i, _p, pname in moved]}
