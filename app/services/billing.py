from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, func
from datetime import datetime

from app.models import User, MeterReading, BillingPeriod, Tariff
from app.services.calculations import calculate_utilities

import logging

logger = logging.getLogger("billing_service")


# --- ЛОГИКА ЗАКРЫТИЯ ПЕРИОДА ---
async def close_current_period(db: AsyncSession, admin_user_id: int):
    """
    1. Находит текущий активный период.
    2. Генерирует показания 'по среднему' для тех, кто не сдал.
    3. Утверждает все зависшие черновики (опционально, или оставляет как есть).
    4. Делает период неактивным.
    """
    # 1. Получаем активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()

    if not active_period:
        raise ValueError("Нет активного периода для закрытия.")

    # 2. Получаем тарифы
    res_tariff = await db.execute(select(Tariff).where(Tariff.id == 1))
    tariff = res_tariff.scalars().first()

    # 3. Ищем пользователей БЕЗ показаний (черновики считаются как "сдал", но их надо утвердить)
    # В этом решении мы считаем "сдавшим" того, у кого есть хоть какая-то запись в этом периоде.

    # Получаем всех жильцов
    res_users = await db.execute(select(User).where(User.role == "user"))
    all_users = res_users.scalars().all()

    # Получаем ID тех, у кого уже есть запись (черновик или утвержденная)
    res_readings = await db.execute(
        select(MeterReading.user_id)
        .where(MeterReading.period_id == active_period.id)
    )
    users_with_readings_ids = set(res_readings.scalars().all())

    generated_count = 0

    for user in all_users:
        if user.id in users_with_readings_ids:
            continue  # У пользователя уже есть показание, пропускаем

        # ЭТО ДОЛЖНИК (вообще нет записи) -> Генерируем по среднему

        # Получаем историю (последние 3 утвержденных)
        last_readings = await db.execute(
            select(MeterReading)
            .where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
            .order_by(MeterReading.created_at.desc())
            .limit(3)
        )
        history = last_readings.scalars().all()

        # Расчет среднего прироста
        if len(history) >= 2:
            deltas_hot = []
            deltas_cold = []
            deltas_elect = []

            for i in range(len(history) - 1):
                curr = history[i]
                prev = history[i + 1]
                deltas_hot.append(max(0, curr.hot_water - prev.hot_water))
                deltas_cold.append(max(0, curr.cold_water - prev.cold_water))
                deltas_elect.append(max(0, curr.electricity - prev.electricity))

            avg_hot = sum(deltas_hot) / len(deltas_hot) if deltas_hot else 0
            avg_cold = sum(deltas_cold) / len(deltas_cold) if deltas_cold else 0
            avg_elect = sum(deltas_elect) / len(deltas_elect) if deltas_elect else 0

            last_reading = history[0]
            new_hot = last_reading.hot_water + avg_hot
            new_cold = last_reading.cold_water + avg_cold
            new_elect = last_reading.electricity + avg_elect
        elif len(history) == 1:
            # Если одна запись, прирост 0
            new_hot = history[0].hot_water
            new_cold = history[0].cold_water
            new_elect = history[0].electricity
        else:
            # Нет истории
            new_hot = 0.0
            new_cold = 0.0
            new_elect = 0.0

        # Расчет денег
        total_residents = user.total_room_residents if user.total_room_residents > 0 else 1
        d_elect_val = new_elect - (history[0].electricity if history else 0)
        user_share_kwh = (user.residents_count / total_residents) * d_elect_val
        vol_sewage = (new_hot - (history[0].hot_water if history else 0)) + \
                     (new_cold - (history[0].cold_water if history else 0))

        costs = calculate_utilities(
            user=user,
            tariff=tariff,
            volume_hot=max(0, new_hot - (history[0].hot_water if history else 0)),
            volume_cold=max(0, new_cold - (history[0].cold_water if history else 0)),
            volume_sewage=max(0, vol_sewage),
            volume_electricity_share=max(0, user_share_kwh)
        )

        auto_reading = MeterReading(
            user_id=user.id,
            period_id=active_period.id,
            hot_water=new_hot,
            cold_water=new_cold,
            electricity=new_elect,
            is_approved=True,  # Сразу утверждаем авто-показания
            anomaly_flags="AUTO_GENERATED",
            total_cost=costs["total_cost"],
            cost_hot_water=costs["cost_hot_water"],
            cost_cold_water=costs["cost_cold_water"],
            cost_sewage=costs["cost_sewage"],
            cost_electricity=costs["cost_electricity"],
            cost_maintenance=costs["cost_maintenance"],
            cost_social_rent=costs["cost_social_rent"],
            cost_waste=costs["cost_waste"],
            cost_fixed_part=costs["cost_fixed_part"],
            created_at=datetime.utcnow()
        )
        db.add(auto_reading)
        generated_count += 1

    # ВАЖНО: Утверждаем все висящие черновики, так как месяц закрывается
    # (Или можно оставить их неутвержденными, но тогда они "застрянут" в закрытом периоде)
    # Для безопасности лучше утвердить их "как есть".
    pending_drafts = await db.execute(
        select(MeterReading).where(MeterReading.period_id == active_period.id, MeterReading.is_approved == False)
    )
    for draft in pending_drafts.scalars().all():
        draft.is_approved = True

    # 4. Делаем период неактивным
    active_period.is_active = False

    await db.commit()
    logger.info(f"Period '{active_period.name}' closed.")

    return {
        "status": "closed",
        "closed_period": active_period.name,
        "auto_generated": generated_count
    }


# --- ЛОГИКА ОТКРЫТИЯ ПЕРИОДА ---
async def open_new_period(db: AsyncSession, new_name: str):
    # Проверяем, нет ли уже активного
    res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    if res.scalars().first():
        raise ValueError("Сначала закройте текущий активный месяц!")

    # Проверяем имя на уникальность
    res_exist = await db.execute(select(BillingPeriod).where(BillingPeriod.name == new_name))
    if res_exist.scalars().first():
        raise ValueError(f"Период с именем '{new_name}' уже существует!")

    new_p = BillingPeriod(name=new_name, is_active=True)
    db.add(new_p)
    await db.commit()

    logger.info(f"New period '{new_name}' opened.")
    return new_p