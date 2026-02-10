# Добавьте эту строку в самый верх файла!
print("--- LOADING CORRECT VERSION OF BILLING.PY (V3) ---")

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime
from decimal import Decimal
import logging

from app.models import User, MeterReading, BillingPeriod, Tariff
from app.services.calculations import calculate_utilities, D

logger = logging.getLogger("billing_service")


# --- ЛОГИКА ЗАКРЫТИЯ ПЕРИОДА ---
async def close_current_period(db: AsyncSession, admin_user_id: int):
    """
    Закрывает текущий расчетный период.
    ВАЖНО: Эта функция теперь ожидает, что транзакция управляется извне (в роутере).
    Она только подготавливает объекты для сохранения в БД.
    """
    # 1. Получаем активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()

    if not active_period:
        raise ValueError("Нет активного периода для закрытия.")

    # 2. Получаем тарифы
    res_tariff = await db.execute(select(Tariff).where(Tariff.id == 1))
    tariff = res_tariff.scalars().first()
    if not tariff:
        raise ValueError("Тарифы не найдены в системе. Невозможно выполнить расчет.")

    # 3. Ищем пользователей БЕЗ показаний
    res_users = await db.execute(select(User).where(User.role == "user"))
    all_users = res_users.scalars().all()

    res_readings = await db.execute(
        select(MeterReading.user_id)
        .where(MeterReading.period_id == active_period.id)
    )
    users_with_readings_ids = set(res_readings.scalars().all())

    generated_count = 0
    zero = Decimal("0.000")

    for user in all_users:
        if user.id in users_with_readings_ids:
            continue

        # --- Генерация по среднему для должника ---
        last_readings = await db.execute(
            select(MeterReading)
            .where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
            .order_by(MeterReading.created_at.desc())
            .limit(3)
        )
        history = last_readings.scalars().all()

        # Расчет среднего прироста
        if len(history) >= 2:
            deltas_hot, deltas_cold, deltas_elect = [], [], []
            for i in range(len(history) - 1):
                curr, prev = history[i], history[i + 1]
                deltas_hot.append(max(zero, D(curr.hot_water) - D(prev.hot_water)))
                deltas_cold.append(max(zero, D(curr.cold_water) - D(prev.cold_water)))
                deltas_elect.append(max(zero, D(curr.electricity) - D(prev.electricity)))

            count = len(deltas_hot)
            avg_hot = sum(deltas_hot) / count if deltas_hot else zero
            avg_cold = sum(deltas_cold) / count if deltas_cold else zero
            avg_elect = sum(deltas_elect) / count if deltas_elect else zero

            last_reading = history[0]
            new_hot = D(last_reading.hot_water) + avg_hot
            new_cold = D(last_reading.cold_water) + avg_cold
            new_elect = D(last_reading.electricity) + avg_elect
        elif len(history) == 1:
            new_hot = D(history[0].hot_water)
            new_cold = D(history[0].cold_water)
            new_elect = D(history[0].electricity)
        else:
            new_hot, new_cold, new_elect = zero, zero, zero

        last_hot_val = D(history[0].hot_water) if history else zero
        last_cold_val = D(history[0].cold_water) if history else zero
        last_elect_val = D(history[0].electricity) if history else zero

        vol_hot = max(zero, new_hot - last_hot_val)
        vol_cold = max(zero, new_cold - last_cold_val)
        d_elect_total = new_elect - last_elect_val

        residents = D(user.residents_count)
        total_residents = D(user.total_room_residents if user.total_room_residents > 0 else 1)
        user_share_kwh = (residents / total_residents) * d_elect_total

        costs = calculate_utilities(
            user=user, tariff=tariff, volume_hot=vol_hot, volume_cold=vol_cold,
            volume_sewage=vol_hot + vol_cold, volume_electricity_share=max(zero, user_share_kwh)
        )

        auto_reading = MeterReading(user_id=user.id, period_id=active_period.id, hot_water=new_hot,
                                    cold_water=new_cold, electricity=new_elect, is_approved=True,
                                    anomaly_flags="AUTO_GENERATED", created_at=datetime.utcnow(), **costs)
        db.add(auto_reading)
        generated_count += 1

    pending_drafts = await db.execute(
        select(MeterReading).where(MeterReading.period_id == active_period.id, MeterReading.is_approved == False)
    )
    for draft in pending_drafts.scalars().all():
        draft.is_approved = True

    active_period.is_active = False

    logger.info(f"Period '{active_period.name}' prepared for closing. Auto-generated: {generated_count}")

    return {"status": "closed", "closed_period": active_period.name, "auto_generated": generated_count}


# --- ЛОГИКА ОТКРЫТИЯ ПЕРИОДА ---
async def open_new_period(db: AsyncSession, new_name: str):
    """Открывает новый расчетный период."""
    res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    if res.scalars().first():
        raise ValueError("Сначала закройте текущий активный месяц!")

    res_exist = await db.execute(select(BillingPeriod).where(BillingPeriod.name == new_name))
    if res_exist.scalars().first():
        raise ValueError(f"Период с именем '{new_name}' уже существует!")

    new_p = BillingPeriod(name=new_name, is_active=True)
    db.add(new_p)

    await db.flush()
    await db.refresh(new_p)

    logger.info(f"New period '{new_name}' opened and prepared for commit.")

    return new_p