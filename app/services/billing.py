# app/services/billing.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, insert, func
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
    Оптимизирован для работы с большим количеством пользователей (Bulk Operations).
    V2: Вместо загрузки всей истории, получаем только последние 4 записи для каждого пользователя.
    """

    # 1. Получаем и блокируем активный период
    result = await db.execute(
        select(BillingPeriod)
        .where(BillingPeriod.is_active.is_(True))
        .with_for_update()
    )
    active_period = result.scalars().first()

    if not active_period:
        raise ValueError("Нет активного периода для закрытия.")

    # 2. Получаем активный тариф
    tariff_result = await db.execute(
        select(Tariff).where(Tariff.is_active == True)
    )
    tariff = tariff_result.scalars().first()
    if not tariff:
        raise ValueError("Активный тариф не найден.")

    # 3. Получаем ID тех, кто УЖЕ сдал показания в этом месяце
    submitted_readings_res = await db.execute(
        select(MeterReading.user_id)
        .where(MeterReading.period_id == active_period.id)
    )
    users_with_readings = set(submitted_readings_res.scalars().all())

    # 4. Получаем список пользователей, которым нужен авто-расчет
    users_to_process_res = await db.execute(
        select(User).where(User.role == "user", User.id.notin_(users_with_readings))
    )
    users_to_process = users_to_process_res.scalars().all()

    # Если все сдали показания, то просто закрываем период
    if not users_to_process:
        active_period.is_active = False
        logger.info("Все пользователи сдали показания. Авто-расчет не требуется.")
        # Все равно нужно утвердить черновики, которые могли быть
        await db.execute(
            update(MeterReading).where(
                MeterReading.period_id == active_period.id, MeterReading.is_approved.is_(False)
            ).values(is_approved=True)
        )
        return {"status": "closed", "closed_period": active_period.name, "auto_generated": 0}

    # 5. SQL-ОПТИМИЗАЦИЯ: Загружаем только последние 4 записи истории для каждого нужного пользователя
    # Это главная оптимизация, которая предотвращает загрузку всей базы в память.
    # Мы используем оконную функцию ROW_NUMBER() для ранжирования записей.

    user_ids_to_fetch = [user.id for user in users_to_process]

    # Создаем подзапрос (CTE) с ранжированными записями
    ranked_readings_subquery = (
        select(
            MeterReading,
            func.row_number().over(
                partition_by=MeterReading.user_id,
                order_by=MeterReading.created_at.desc()
            ).label("row_num")
        )
        .where(
            MeterReading.user_id.in_(user_ids_to_fetch),
            MeterReading.is_approved.is_(True)
        )
        .subquery()
    )

    # Выбираем только те, у которых ранг от 1 до 4
    recent_history_stmt = select(ranked_readings_subquery).where(
        ranked_readings_subquery.c.row_num <= 4
    )

    recent_history_result = await db.execute(recent_history_stmt)

    # Группируем полученную ограниченную историю в памяти
    history_map = defaultdict(list)
    for row in recent_history_result.all():
        # SQLAlchemy возвращает объект MeterReading в row[0] при выборке из subquery
        reading_obj = MeterReading(**{c.name: getattr(row, c.name) for c in MeterReading.__table__.columns})
        history_map[row.user_id].append(reading_obj)

    # --- Дальнейший код почти не меняется, но теперь он работает с маленьким объемом данных ---

    zero = Decimal("0.000")
    zero_money = Decimal("0.00")
    insert_values = []
    generated_count = 0

    # 6. Главный цикл расчета (теперь быстрый, т.к. history_map маленький)
    for user in users_to_process:
        history = history_map.get(user.id, [])
        # Сортируем на всякий случай, если БД вернула не в том порядке
        history.sort(key=lambda r: r.created_at, reverse=True)

        # --- Логика авто-расчета по среднему ---
        if len(history) >= 2:
            deltas_hot, deltas_cold, deltas_elect = [], [], []
            for i in range(len(history) - 1):
                curr, prev = history[i], history[i + 1]
                deltas_hot.append(max(zero, D(curr.hot_water) - D(prev.hot_water)))
                deltas_cold.append(max(zero, D(curr.cold_water) - D(prev.cold_water)))
                deltas_elect.append(max(zero, D(curr.electricity) - D(prev.electricity)))

            count = len(deltas_hot)
            avg_hot = sum(deltas_hot) / count if count > 0 else zero
            avg_cold = sum(deltas_cold) / count if count > 0 else zero
            avg_elect = sum(deltas_elect) / count if count > 0 else zero

            last = history[0]
            new_hot = D(last.hot_water) + avg_hot
            new_cold = D(last.cold_water) + avg_cold
            new_elect = D(last.electricity) + avg_elect

        # Если история есть, но всего одна запись, повторяем её (расход 0)
        elif len(history) == 1:
            last = history[0]
            new_hot, new_cold, new_elect = D(last.hot_water), D(last.cold_water), D(last.electricity)

        # Если истории нет вообще (новый пользователь), ставим 0
        else:
            new_hot, new_cold, new_elect = zero, zero, zero

        # --- Расчет стоимости ---
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
            user=user, tariff=tariff, volume_hot=vol_hot, volume_cold=vol_cold,
            volume_sewage=vol_hot + vol_cold, volume_electricity_share=max(zero, share_kwh)
        )

        cost_rent_205 = costs['cost_social_rent']
        cost_utils_209 = costs['total_cost'] - cost_rent_205

        row = {
            "user_id": user.id, "period_id": active_period.id,
            "hot_water": new_hot, "cold_water": new_cold, "electricity": new_elect,
            "debt_209": zero_money, "overpayment_209": zero_money,
            "debt_205": zero_money, "overpayment_205": zero_money,
            "total_209": cost_utils_209, "total_205": cost_rent_205,
            "is_approved": True, "anomaly_flags": "AUTO_GENERATED",
            "created_at": datetime.utcnow(),
            **costs
        }
        insert_values.append(row)
        generated_count += 1

    # 7. MASSIVE INSERT (БЫСТРАЯ ВСТАВКА)
    if insert_values:
        chunk_size = 1000
        for i in range(0, len(insert_values), chunk_size):
            chunk = insert_values[i: i + chunk_size]
            await db.execute(insert(MeterReading), chunk)
            logger.info(f"Inserted chunk {i} to {i + len(chunk)}")

    # 8. MASSIVE UPDATE (БЫСТРОЕ ОБНОВЛЕНИЕ ЧЕРНОВИКОВ)
    await db.execute(
        update(MeterReading).where(
            MeterReading.period_id == active_period.id, MeterReading.is_approved.is_(False)
        ).values(is_approved=True)
    )

    # 9. Закрываем период
    active_period.is_active = False

    logger.info(f"Period '{active_period.name}' prepared for closing. Auto-generated (Bulk): {generated_count}")
    return {"status": "closed", "closed_period": active_period.name, "auto_generated": generated_count}


async def open_new_period(db: AsyncSession, new_name: str):
    """
    Открывает новый расчетный период.
    """
    active_result = await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True)).with_for_update()
    )
    if active_result.scalars().first():
        raise ValueError("Сначала закройте текущий активный месяц.")

    exist_result = await db.execute(select(BillingPeriod).where(BillingPeriod.name == new_name))
    if exist_result.scalars().first():
        raise ValueError(f"Период с именем '{new_name}' уже существует.")

    new_period = BillingPeriod(name=new_name, is_active=True, created_at=datetime.utcnow())
    db.add(new_period)
    await db.flush()
    await db.refresh(new_period)
    logger.info(f"New period '{new_name}' opened.")
    return new_period