# app/modules/utility/services/singles_billing.py
"""Тиражирование квитанции по ХОЛОСТЯЦКОЙ квартире.

Холостяки в одной квартире делят коммуналку ПОРОВНУ. Счётчики физически
одни на квартиру; calculate_utilities уже делит метраж/расход на
room.total_room_residents — то есть посчитанная квитанция ОДНОГО жильца это
уже доля одного человека. Чтобы остальные жильцы получили ту же долю (а не
голый baseline ~площадь), её надо СКОПИРОВАТЬ на всех остальных активных
жильцов квартиры за период.

Тиражирование изначально жило только в подаче жильцом (client_readings).
Админские пути — ручной ввод и Excel-импорт — его не делали, из-за чего
введённому начислялась его доля, а остальным только baseline. Этот helper —
единая точка для админских путей.
"""
from decimal import Decimal
from typing import Optional

from sqlalchemy.future import select

from app.modules.utility.models import User, MeterReading
from app.modules.utility.services.calculations import MODEL_COST_FIELDS

ZERO = Decimal("0.00")
SINGLES_FLAG = "SINGLES_SHARED"


def _with_singles_flag(flags: Optional[str]) -> str:
    """Гарантирует наличие маркера SINGLES_SHARED во флагах (без дублей)."""
    base = (flags or "").strip()
    parts = [p for p in base.replace("|", ",").split(",") if p.strip()]
    if SINGLES_FLAG not in parts:
        parts.append(SINGLES_FLAG)
    return ",".join(parts)


async def propagate_singles_reading(
    db,
    *,
    room,
    period_id: int,
    source_user_id: int,
    hot: Decimal,
    cold: Decimal,
    elect: Decimal,
    costs: dict,
    total_209: Decimal,
    total_205: Decimal,
    flags: Optional[str],
    is_approved: bool,
    exclude_user_ids: Optional[set] = None,
) -> list:
    """Копирует уже посчитанную (делёную) квитанцию на ВСЕХ остальных активных
    жильцов холостяцкой квартиры за период (upsert). Возвращает список
    затронутых жильцов (User). Если комната не холостяцкая — ничего не делает.

    costs — словарь из calculate_utilities (содержит cost_* + total_cost).
    is_approved — статус создаваемых/обновляемых клонов (для активного периода
    черновики, для закрытого/Excel — утверждённые).
    """
    if not bool(getattr(room, "is_singles_apartment", False)):
        return []

    exclude = set(exclude_user_ids or set())
    exclude.add(source_user_id)

    others = (await db.execute(
        select(User).where(
            User.room_id == room.id,
            User.is_deleted.is_(False),
            User.role == "user",
            User.id.notin_(exclude),
        )
    )).scalars().all()
    if not others:
        return []

    payload = {
        "hot_water": hot, "cold_water": cold, "electricity": elect,
        "total_209": total_209, "total_205": total_205,
        "total_cost": total_209 + total_205,
        "anomaly_flags": _with_singles_flag(flags),
        "anomaly_score": 0,
    }
    for f in MODEL_COST_FIELDS:
        if f in costs:
            payload[f] = costs[f]

    affected = []
    for ou in others:
        # Ищем показание соседа за период В ЭТОЙ ЖЕ комнате (фильтр room_id —
        # иначе при переезде внутри периода зацепили бы reading старой комнаты).
        existing = (await db.execute(
            select(MeterReading).where(
                MeterReading.user_id == ou.id,
                MeterReading.room_id == room.id,
                MeterReading.period_id == period_id,
            ).order_by(MeterReading.is_approved.desc(), MeterReading.id.desc()).limit(1)
        )).scalars().first()

        # ХОЛОСТЯЦКАЯ квартира = ОДИН счётчик на всех, счёт делится ПОРОВНУ.
        # «Своё» отдельное показание соседа здесь не имеет смысла — выравниваем
        # его долю под источник (даже если оно было утверждено). НЕ трогаем
        # только сальдо 1С: debt_*/overpayment_* в payload не входят и
        # сохраняются как есть. Это и есть требование «начислять всем поровну».
        if existing:
            for k, v in payload.items():
                setattr(existing, k, v)
            if is_approved:
                existing.is_approved = True
            existing.edit_count = (existing.edit_count or 0) + 1
            db.add(existing)
        else:
            db.add(MeterReading(
                user_id=ou.id, room_id=room.id, period_id=period_id,
                debt_209=ZERO, overpayment_209=ZERO,
                debt_205=ZERO, overpayment_205=ZERO,
                is_approved=is_approved, edit_count=1, edit_history=[],
                **payload,
            ))
        affected.append(ou)

    return affected


async def equalize_singles_room(db, *, room, period_id: int) -> dict:
    """Чинит УЖЕ заведённые холостяцкие квартиры: пересчитывает квитанцию
    «апартамента» по ОБЩЕМУ счётчику и раскидывает РАВНУЮ долю на всех жильцов.

    Зачем: исторически жильцы холостяцкой квартиры могли получить НЕзависимые
    показания (один — с расходом, другой — baseline), потому что тиражирование
    в админ-путях не работало. Плюс у части комнат total_room_residents был
    устаревший (делитель неверный). Эта функция:
      1) берёт ИСТОЧНИК = показание жильца с максимальной суммой счётчиков
         (= фактическое текущее состояние общего счётчика квартиры);
      2) пересчитывает его разбивку через канонический compute_reading_breakdown
         с актуальным total_room_residents (делёж счёта на N);
      3) копирует РАВНУЮ долю на всех жильцов квартиры (propagate).

    Возвращает dict с тем, что сделано. Сальдо 1С (debt/overpayment) не трогаем.
    Вызывающий обязан ПЕРЕД этим вызвать recount_singles_residents (делитель).
    """
    from app.modules.utility.services.reading_calculator import (
        compute_reading_breakdown, is_meaningful_prev,
    )
    from app.modules.utility.services.period_helpers import period_chron_key
    from app.modules.utility.services.tariff_cache import tariff_cache
    from app.modules.utility.routers.settings import _load_seasonal
    from app.modules.utility.models import Adjustment, BillingPeriod
    from sqlalchemy import func

    if not bool(getattr(room, "is_singles_apartment", False)):
        return {"room_id": room.id, "status": "skip", "reason": "not singles"}

    residents = (await db.execute(
        select(User).where(
            User.room_id == room.id, User.is_deleted.is_(False), User.role == "user",
        )
    )).scalars().all()
    if len(residents) < 2:
        return {"room_id": room.id, "status": "skip", "reason": "<2 residents"}

    # По одному (последнему) утверждённому показанию на жильца за период.
    present = []
    for u in residents:
        r = (await db.execute(
            select(MeterReading).where(
                MeterReading.user_id == u.id,
                MeterReading.room_id == room.id,
                MeterReading.period_id == period_id,
                MeterReading.is_approved.is_(True),
            ).order_by(MeterReading.id.desc()).limit(1)
        )).scalars().first()
        if r is not None:
            present.append((u, r))
    if not present:
        return {"room_id": room.id, "status": "skip", "reason": "no approved readings"}

    # Источник = максимальная сумма счётчиков (актуальное состояние общего счётчика).
    def _msum(rd):
        return (rd.hot_water or ZERO) + (rd.cold_water or ZERO) + (rd.electricity or ZERO)
    source_user, source = max(present, key=lambda ur: _msum(ur[1]))

    tariff = tariff_cache.get_effective_tariff(user=source_user, room=room)
    if tariff is None:
        return {"room_id": room.id, "status": "skip", "reason": "no tariff"}

    # prev источника — последнее ОСМЫСЛЕННОЕ approved-показание в этой комнате
    # ХРОНОЛОГИЧЕСКИ раньше периода (для дельты расхода).
    period = await db.get(BillingPeriod, period_id)
    cur_key = period_chron_key(period.name) if period else None
    prev_rows = (await db.execute(
        select(MeterReading, BillingPeriod)
        .join(BillingPeriod, MeterReading.period_id == BillingPeriod.id)
        .where(
            MeterReading.user_id == source_user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved.is_(True),
        )
    )).all()
    prev_reading = None
    if cur_key is not None:
        earlier = [(mr, period_chron_key(p.name)) for mr, p in prev_rows]
        earlier = [(mr, k) for mr, k in earlier if k is not None and k < cur_key and is_meaningful_prev(mr)]
        earlier.sort(key=lambda x: x[1])
        if earlier:
            prev_reading = earlier[-1][0]

    seasonal = await _load_seasonal(db)
    heating = seasonal.heating_season_active and tariff.is_heating_active_now()
    hw = seasonal.hot_water_heating_active and tariff.is_hw_heating_active_now()

    bd = compute_reading_breakdown(
        user=source_user, room=room, tariff=tariff,
        current_hot=source.hot_water, current_cold=source.cold_water,
        current_elect=source.electricity, prev_reading=prev_reading,
        heating_season_active=heating, hot_water_heating_active=hw,
    )
    # Корректировки источника за период (если есть) — кладём только на источник,
    # на клонах равная КОММУНАЛЬНАЯ доля без чужих корректировок.
    adj_map = {row[0]: (row[1] or ZERO) for row in (await db.execute(
        select(Adjustment.account_type, func.sum(Adjustment.amount))
        .where(Adjustment.user_id == source_user.id, Adjustment.period_id == period_id)
        .group_by(Adjustment.account_type)
    )).all()}
    base_209, base_205 = bd["total_209"], bd["total_205"]

    # Применяем к ИСТОЧНИКУ (с его корректировками).
    for f in MODEL_COST_FIELDS:
        if f in bd:
            setattr(source, f, bd[f])
    s209 = base_209 + adj_map.get("209", ZERO)
    s205 = base_205 + adj_map.get("205", ZERO)
    source.total_209, source.total_205, source.total_cost = s209, s205, s209 + s205
    source.anomaly_flags = _with_singles_flag(source.anomaly_flags)
    db.add(source)

    # Раскидываем РАВНУЮ долю (без корректировок источника) на остальных.
    affected = await propagate_singles_reading(
        db, room=room, period_id=period_id, source_user_id=source_user.id,
        hot=source.hot_water, cold=source.cold_water, elect=source.electricity,
        costs=bd, total_209=base_209, total_205=base_205,
        flags=source.anomaly_flags, is_approved=True,
    )
    return {
        "room_id": room.id, "status": "equalized",
        "source_user_id": source_user.id,
        "share_209": float(base_209), "share_205": float(base_205),
        "residents": len(residents), "propagated": len(affected),
    }
