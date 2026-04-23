# app/modules/utility/services/admin_readings_manual.py
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, desc
from sqlalchemy.orm import selectinload

from app.modules.utility.models import User, MeterReading, Room, Tariff, BillingPeriod, Adjustment
from app.modules.utility.schemas import AdminManualReadingSchema, OneTimeChargeSchema
from app.modules.utility.services.calculations import calculate_utilities
from app.modules.utility.services.anomaly_detector import check_reading_for_anomalies_v2

ZERO = Decimal("0.00")

async def save_manual_entry(db: AsyncSession, data: AdminManualReadingSchema):
    """Сохранение черновика бухгалтером вручную."""
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))).scalars().first()
    if not active_period: raise HTTPException(status_code=400, detail="Расчетный период закрыт.")

    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == data.user_id))).scalars().first()
    if not user or user.is_deleted: raise HTTPException(status_code=404, detail="Жилец не найден")

    room = user.room
    if not room: raise HTTPException(status_code=400, detail="Жилец не привязан к помещению")

    # Через единый кеш — Room.tariff_id побеждает User.tariff_id (см. tariff_cache.py).
    from app.modules.utility.services.tariff_cache import tariff_cache
    t = tariff_cache.get_effective_tariff(user=user, room=room) or \
        (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()

    # История считается ПО ЖИЛЬЦУ В ЭТОЙ КОМНАТЕ, не по комнате в целом.
    # Если до этого жильца тут были показания (старый жилец, GSHEETS_AUTO
    # и т.п.), их учитывать нельзя — получились бы миллионы.
    history = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved,
        )
        .order_by(MeterReading.created_at.desc()).limit(6)
    )).scalars().all()

    prev_latest = history[0] if history else None
    prev_manual = next((r for r in history if r.anomaly_flags != "AUTO_GENERATED"), None)

    p_hot_man, p_cold_man, p_elect_man = prev_manual.hot_water if prev_manual else ZERO, prev_manual.cold_water if prev_manual else ZERO, prev_manual.electricity if prev_manual else ZERO

    if data.hot_water < p_hot_man or data.cold_water < p_cold_man or data.electricity < p_elect_man:
        raise HTTPException(400, "Новые показания не могут быть меньше реально переданных ранее!")

    p_hot, p_cold, p_elect = prev_latest.hot_water if prev_latest else ZERO, prev_latest.cold_water if prev_latest else ZERO, prev_latest.electricity if prev_latest else ZERO
    d_hot, d_cold, d_elect = data.hot_water - p_hot, data.cold_water - p_cold, data.electricity - p_elect

    residents_count = user.residents_count if user.residents_count is not None else 1
    total_room = room.total_room_residents if room.total_room_residents > 0 else 1

    user_share_elect = (Decimal(residents_count) / Decimal(total_room)) * d_elect

    # BASELINE: если по комнате нет утверждённой истории — первая подача,
    # все cost_* = 0 (счётчики могут быть «накрученные» за годы, см. также
    # approve_single / bulk_approve_drafts / client save_reading). Флаг
    # BASELINE попадёт в реестр, чтобы админ не искал «откуда ноль».
    is_baseline = prev_latest is None
    if is_baseline:
        costs = {
            "cost_hot_water": ZERO, "cost_cold_water": ZERO,
            "cost_sewage": ZERO, "cost_electricity": ZERO,
            "cost_maintenance": ZERO, "cost_social_rent": ZERO,
            "cost_waste": ZERO, "cost_fixed_part": ZERO,
            "total_cost": ZERO,
        }
    else:
        costs = calculate_utilities(
            user=user, room=room, tariff=t, volume_hot=d_hot, volume_cold=d_cold,
            volume_sewage=d_hot + d_cold, volume_electricity_share=user_share_elect
        )

    temp_reading = MeterReading(hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity)
    flags, score = check_reading_for_anomalies_v2(temp_reading, history, user=user)
    if is_baseline:
        flags, score = "BASELINE", 0

    adj_map = {row[0]: (row[1] or ZERO) for row in
               (await db.execute(select(Adjustment.account_type, func.sum(Adjustment.amount))
               .where(Adjustment.user_id == user.id, Adjustment.period_id == active_period.id).group_by(Adjustment.account_type))).all()}

    draft = (await db.execute(
        select(MeterReading).where(MeterReading.room_id == room.id, MeterReading.is_approved.is_(False), MeterReading.period_id == active_period.id)
    )).scalars().first()

    total_209 = (costs['total_cost'] - costs['cost_social_rent']) + (draft.debt_209 or ZERO if draft else ZERO) - (draft.overpayment_209 or ZERO if draft else ZERO) + adj_map.get('209', ZERO)
    total_205 = costs['cost_social_rent'] + (draft.debt_205 or ZERO if draft else ZERO) - (draft.overpayment_205 or ZERO if draft else ZERO) + adj_map.get('205', ZERO)

    if draft:
        draft.hot_water, draft.cold_water, draft.electricity = data.hot_water, data.cold_water, data.electricity
        draft.anomaly_flags, draft.anomaly_score = flags, score
        for k, v in costs.items():
            if hasattr(draft, k): setattr(draft, k, v)
        draft.total_209, draft.total_205, draft.total_cost = total_209, total_205, total_209 + total_205
    else:
        costs.pop('total_cost', None)
        db.add(MeterReading(
            user_id=user.id, room_id=room.id, period_id=active_period.id,
            hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity,
            debt_209=ZERO, overpayment_209=ZERO, debt_205=ZERO, overpayment_205=ZERO,
            total_209=total_209, total_205=total_205, total_cost=total_209 + total_205,
            is_approved=False, anomaly_flags=flags, anomaly_score=score, **costs
        ))

    await db.commit()
    return {"status": "success"}


async def create_one_time_charge(db: AsyncSession, data: OneTimeChargeSchema):
    """Разовое (пропорциональное) начисление при выселении или переезде."""
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))).scalars().first()
    if not active_period: raise HTTPException(status_code=400, detail="Нет активного периода")

    user = (await db.execute(select(User).options(selectinload(User.room)).where(User.id == data.user_id))).scalars().first()
    if not user or user.is_deleted: raise HTTPException(status_code=404, detail="Жилец не найден")

    room = user.room
    if not room: raise HTTPException(status_code=400, detail="Жилец не привязан к помещению")

    if data.total_days_in_month <= 0 or data.days_lived < 0 or data.days_lived > data.total_days_in_month:
        raise HTTPException(status_code=400, detail="Неверно указаны дни проживания")

    fraction = Decimal(data.days_lived) / Decimal(data.total_days_in_month)

    # Через единый кеш — Room.tariff_id побеждает User.tariff_id (см. tariff_cache.py).
    from app.modules.utility.services.tariff_cache import tariff_cache
    t = tariff_cache.get_effective_tariff(user=user, room=room) or \
        (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()

    # История по ЖИЛЬЦУ В ЭТОЙ КОМНАТЕ (см. save_manual_entry выше).
    history = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved,
        )
        .order_by(MeterReading.created_at.desc()).limit(6)
    )).scalars().all()

    prev_latest = history[0] if history else None
    prev_manual = next((r for r in history if r.anomaly_flags != "AUTO_GENERATED"), None)

    p_hot_man, p_cold_man, p_elect_man = prev_manual.hot_water if prev_manual else ZERO, prev_manual.cold_water if prev_manual else ZERO, prev_manual.electricity if prev_manual else ZERO

    if data.hot_water < p_hot_man or data.cold_water < p_cold_man or data.electricity < p_elect_man:
        raise HTTPException(400, "Новые показания не могут быть меньше реально переданных ранее!")

    p_hot, p_cold, p_elect = prev_latest.hot_water if prev_latest else ZERO, prev_latest.cold_water if prev_latest else ZERO, prev_latest.electricity if prev_latest else ZERO
    d_hot, d_cold, d_elect = data.hot_water - p_hot, data.cold_water - p_cold, data.electricity - p_elect

    residents_count = user.residents_count if user.residents_count is not None else 1
    total_room = room.total_room_residents if room.total_room_residents > 0 else 1

    user_share_elect = (Decimal(residents_count) / Decimal(total_room)) * d_elect

    # BASELINE: первая в жизни подача по комнате → 0 (см. комментарий выше).
    # Для разового начисления это редкий сценарий (обычно у выселяющегося
    # уже есть история), но технически возможен — страхуемся.
    is_baseline = prev_latest is None
    if is_baseline:
        costs = {
            "cost_hot_water": ZERO, "cost_cold_water": ZERO,
            "cost_sewage": ZERO, "cost_electricity": ZERO,
            "cost_maintenance": ZERO, "cost_social_rent": ZERO,
            "cost_waste": ZERO, "cost_fixed_part": ZERO,
            "total_cost": ZERO,
        }
    else:
        costs = calculate_utilities(
            user=user, room=room, tariff=t, volume_hot=d_hot, volume_cold=d_cold,
            volume_sewage=d_hot + d_cold, volume_electricity_share=user_share_elect, fraction=fraction
        )

    adj_map = {row[0]: (row[1] or ZERO) for row in
               (await db.execute(select(Adjustment.account_type, func.sum(Adjustment.amount))
               .where(Adjustment.user_id == user.id, Adjustment.period_id == active_period.id).group_by(Adjustment.account_type))).all()}

    draft = (await db.execute(
        select(MeterReading).where(MeterReading.room_id == room.id, MeterReading.is_approved.is_(False), MeterReading.period_id == active_period.id)
    )).scalars().first()

    total_209 = (costs['total_cost'] - costs['cost_social_rent']) + (draft.debt_209 or ZERO if draft else ZERO) - (draft.overpayment_209 or ZERO if draft else ZERO) + adj_map.get('209', ZERO)
    total_205 = costs['cost_social_rent'] + (draft.debt_205 or ZERO if draft else ZERO) - (draft.overpayment_205 or ZERO if draft else ZERO) + adj_map.get('205', ZERO)

    charge_flag = "ONE_TIME_CHARGE_BASELINE" if is_baseline else "ONE_TIME_CHARGE"
    if draft:
        draft.hot_water, draft.cold_water, draft.electricity = data.hot_water, data.cold_water, data.electricity
        draft.anomaly_flags, draft.anomaly_score = charge_flag, 0
        for k, v in costs.items(): setattr(draft, k, v)
        draft.total_209, draft.total_205, draft.total_cost, draft.is_approved = total_209, total_205, total_209 + total_205, True
    else:
        costs.pop('total_cost', None)
        db.add(MeterReading(
            user_id=user.id, room_id=room.id, period_id=active_period.id,
            hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity,
            debt_209=ZERO, overpayment_209=ZERO, debt_205=ZERO, overpayment_205=ZERO,
            total_209=total_209, total_205=total_205, total_cost=total_209 + total_205,
            is_approved=True, anomaly_flags=charge_flag, anomaly_score=0, **costs
        ))

    room.last_hot_water, room.last_cold_water, room.last_electricity = data.hot_water, data.cold_water, data.electricity
    db.add(room)

    if data.is_moving_out:
        user.is_deleted = True
        user.username = f"{user.username}_deleted_{user.id}"
        user.room_id = None

    await db.commit()
    return {"status": "success"}


async def delete_reading(db: AsyncSession, reading_id: int):
    reading = await db.get(MeterReading, reading_id)
    if not reading: raise HTTPException(status_code=404, detail="Запись не найдена")
    await db.delete(reading)
    await db.commit()
    return {"status": "deleted"}