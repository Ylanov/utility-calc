from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from datetime import datetime
from decimal import Decimal
import logging
from collections import defaultdict

from app.models import User, MeterReading, BillingPeriod, Tariff
from app.services.calculations import calculate_utilities, D

logger = logging.getLogger("billing_service")


async def close_current_period(db: AsyncSession, admin_user_id: int):
    """
    Закрывает текущий расчетный период.
    Ожидает, что транзакция управляется извне.
    """

    result = await db.execute(
        select(BillingPeriod)
        .where(BillingPeriod.is_active.is_(True))
        .with_for_update()
    )
    active_period = result.scalars().first()

    if not active_period:
        raise ValueError("Нет активного периода для закрытия.")

    # --- ИСПРАВЛЕНИЕ: Ищем просто активный тариф, так как в модели периода нет ссылки ---
    tariff_result = await db.execute(
        select(Tariff).where(Tariff.is_active == True)
    )
    tariff = tariff_result.scalars().first()

    if not tariff:
        raise ValueError("Активный тариф не найден.")
    # ------------------------------------------------------------------------------------

    users_result = await db.execute(
        select(User).where(User.role == "user")
    )
    all_users = users_result.scalars().all()

    readings_result = await db.execute(
        select(MeterReading)
        .where(
            MeterReading.period_id == active_period.id
        )
    )
    period_readings = readings_result.scalars().all()

    users_with_readings = {r.user_id for r in period_readings}

    history_result = await db.execute(
        select(MeterReading)
        .where(MeterReading.is_approved.is_(True))
        .order_by(MeterReading.user_id, MeterReading.created_at.desc())
    )
    all_history = history_result.scalars().all()

    history_map = defaultdict(list)
    for reading in all_history:
        if len(history_map[reading.user_id]) < 3:
            history_map[reading.user_id].append(reading)

    zero = Decimal("0.000")
    generated_count = 0

    for user in all_users:
        if user.id in users_with_readings:
            continue

        history = history_map.get(user.id, [])

        if len(history) >= 2:
            deltas_hot = []
            deltas_cold = []
            deltas_elect = []

            for i in range(len(history) - 1):
                curr = history[i]
                prev = history[i + 1]
                deltas_hot.append(max(zero, D(curr.hot_water) - D(prev.hot_water)))
                deltas_cold.append(max(zero, D(curr.cold_water) - D(prev.cold_water)))
                deltas_elect.append(max(zero, D(curr.electricity) - D(prev.electricity)))

            count = len(deltas_hot)
            avg_hot = sum(deltas_hot) / count if deltas_hot else zero
            avg_cold = sum(deltas_cold) / count if deltas_cold else zero
            avg_elect = sum(deltas_elect) / count if deltas_elect else zero

            last = history[0]
            new_hot = D(last.hot_water) + avg_hot
            new_cold = D(last.cold_water) + avg_cold
            new_elect = D(last.electricity) + avg_elect

        elif len(history) == 1:
            last = history[0]
            new_hot = D(last.hot_water)
            new_cold = D(last.cold_water)
            new_elect = D(last.electricity)
        else:
            new_hot = zero
            new_cold = zero
            new_elect = zero

        last_hot = D(history[0].hot_water) if history else zero
        last_cold = D(history[0].cold_water) if history else zero
        last_elect = D(history[0].electricity) if history else zero

        vol_hot = max(zero, new_hot - last_hot)
        vol_cold = max(zero, new_cold - last_cold)
        delta_elect = new_elect - last_elect

        residents = D(user.residents_count)
        total_residents = D(user.total_room_residents if user.total_room_residents > 0 else 1)
        share_kwh = (residents / total_residents) * delta_elect

        costs = calculate_utilities(
            user=user,
            tariff=tariff,
            volume_hot=vol_hot,
            volume_cold=vol_cold,
            volume_sewage=vol_hot + vol_cold,
            volume_electricity_share=max(zero, share_kwh)
        )

        new_reading = MeterReading(
            user_id=user.id,
            period_id=active_period.id,
            hot_water=new_hot,
            cold_water=new_cold,
            electricity=new_elect,
            is_approved=True,
            anomaly_flags="AUTO_GENERATED",
            created_at=datetime.utcnow(),
            **costs
        )

        db.add(new_reading)
        generated_count += 1

    draft_result = await db.execute(
        select(MeterReading)
        .where(
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(False)
        )
    )
    drafts = draft_result.scalars().all()

    for draft in drafts:
        draft.is_approved = True

    active_period.is_active = False

    logger.info(
        f"Period '{active_period.name}' prepared for closing. "
        f"Auto-generated: {generated_count}"
    )

    return {
        "status": "closed",
        "closed_period": active_period.name,
        "auto_generated": generated_count
    }


async def open_new_period(db: AsyncSession, new_name: str):
    """
    Открывает новый расчетный период.
    """

    active_result = await db.execute(
        select(BillingPeriod)
        .where(BillingPeriod.is_active.is_(True))
        .with_for_update()
    )

    if active_result.scalars().first():
        raise ValueError("Сначала закройте текущий активный месяц.")

    exist_result = await db.execute(
        select(BillingPeriod)
        .where(BillingPeriod.name == new_name)
    )

    if exist_result.scalars().first():
        raise ValueError(f"Период с именем '{new_name}' уже существует.")

    new_period = BillingPeriod(
        name=new_name,
        is_active=True,
        created_at=datetime.utcnow()
    )

    db.add(new_period)
    await db.flush()
    await db.refresh(new_period)

    logger.info(f"New period '{new_name}' opened.")

    return new_period