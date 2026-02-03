from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, func
from datetime import datetime

from app.models import User, MeterReading, BillingPeriod, Tariff
from app.services.calculations import calculate_utilities

import logging

logger = logging.getLogger("billing_service")


async def close_period_and_generate_missing(db: AsyncSession, new_period_name: str, admin_user_id: int):
    """
    1. Находит текущий активный период.
    2. Находит пользователей, которые НЕ сдали показания.
    3. Генерирует им показания 'по среднему' за последние 3 месяца.
    4. Закрывает текущий период.
    5. Открывает новый период.
    """

    # 1. Получаем активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()

    if not active_period:
        # Если периода нет, просто создаем новый
        new_p = BillingPeriod(name=new_period_name, is_active=True)
        db.add(new_p)
        await db.commit()
        return {"status": "created_initial", "period": new_p}

    # 2. Получаем тарифы
    res_tariff = await db.execute(select(Tariff).where(Tariff.id == 1))
    tariff = res_tariff.scalars().first()

    # 3. Ищем пользователей БЕЗ показаний в этом периоде
    # Получаем ID всех пользователей
    res_users = await db.execute(select(User).where(User.role == "user"))  # Только жильцы
    all_users = res_users.scalars().all()

    # Получаем ID тех, кто сдал (или кому уже создали черновик)
    res_readings = await db.execute(
        select(MeterReading.user_id)
        .where(MeterReading.period_id == active_period.id)
    )
    users_with_readings_ids = set(res_readings.scalars().all())

    generated_count = 0

    for user in all_users:
        if user.id in users_with_readings_ids:
            continue

        # ЭТО ДОЛЖНИК (не подал показания)
        # 4. Считаем среднее потребление за 3 последних закрытых месяца
        last_readings = await db.execute(
            select(MeterReading)
            .where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
            .order_by(MeterReading.created_at.desc())
            .limit(3)
        )
        history = last_readings.scalars().all()

        avg_hot = 0.0
        avg_cold = 0.0
        avg_elect = 0.0

        # Если история есть, считаем дельты
        if len(history) >= 2:
            # Берем разницу между самым свежим и самым старым в выборке / кол-во месяцев
            # Упрощенно: берем среднее арифметическое начислений (cost), но нам нужны объемы
            # Сложный момент: у нас хранятся накопительные итоги (счетчик).
            # Нам нужно предсказать СЛЕДУЮЩЕЕ значение счетчика.

            # Самое последнее показание (база для нового)
            last_reading = history[0]

            # Считаем средний прирост
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

            # Новые показания = Последнее + Среднее
            new_hot = last_reading.hot_water + avg_hot
            new_cold = last_reading.cold_water + avg_cold
            new_elect = last_reading.electricity + avg_elect

        elif len(history) == 1:
            # Если только 1 запись, считаем прирост 0 (или можно нормативы внедрить)
            new_hot = history[0].hot_water
            new_cold = history[0].cold_water
            new_elect = history[0].electricity
        else:
            # Истории нет вообще - ставим нули
            new_hot = 0.0
            new_cold = 0.0
            new_elect = 0.0

        # Рассчитываем стоимость
        # Т.к. это "по среднему", ставим коррекции в 0

        # Доля электричества
        total_residents = user.total_room_residents if user.total_room_residents > 0 else 1
        # Объем электричества (прирост)
        d_elect_val = new_elect - (history[0].electricity if history else 0)
        user_share_kwh = (user.residents_count / total_residents) * d_elect_val

        # Объем воды (прирост)
        d_hot_val = new_hot - (history[0].hot_water if history else 0)
        d_cold_val = new_cold - (history[0].cold_water if history else 0)
        vol_sewage = d_hot_val + d_cold_val

        costs = calculate_utilities(
            user=user,
            tariff=tariff,
            volume_hot=d_hot_val,
            volume_cold=d_cold_val,
            volume_sewage=vol_sewage,
            volume_electricity_share=user_share_kwh
        )

        # Создаем запись (Сразу утвержденную, т.к. месяц закрывается)
        auto_reading = MeterReading(
            user_id=user.id,
            period_id=active_period.id,
            hot_water=new_hot,
            cold_water=new_cold,
            electricity=new_elect,

            # Пишем, что это автоматический расчет (можно добавить поле comment в модель, но пока так)
            is_approved=True,

            # Заполняем финансы
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

    # 5. Закрываем старый период
    active_period.is_active = False

    # 6. Создаем новый
    new_period = BillingPeriod(name=new_period_name, is_active=True)
    db.add(new_period)

    await db.commit()

    logger.info(f"Period closed. Auto-generated {generated_count} readings.")
    return {
        "status": "closed_and_opened",
        "old_period": active_period.name,
        "new_period": new_period.name,
        "auto_generated_count": generated_count
    }