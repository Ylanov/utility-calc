from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, insert
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

    # 3. Загружаем пользователей и существующие показания
    # Для 5000 пользователей это быстро (ок. 10-50мс)
    users_result = await db.execute(select(User).where(User.role == "user"))
    all_users = users_result.scalars().all()

    # Получаем ID тех, кто УЖЕ сдал показания в этом месяце
    readings_result = await db.execute(
        select(MeterReading.user_id)
        .where(MeterReading.period_id == active_period.id)
    )
    # Используем set для мгновенного поиска O(1)
    users_with_readings = set(readings_result.scalars().all())

    # 4. Загружаем историю для авто-расчета среднего
    # Загружаем только утвержденные показания
    history_result = await db.execute(
        select(MeterReading)
        .where(MeterReading.is_approved.is_(True))
        .order_by(MeterReading.user_id, MeterReading.created_at.desc())
    )
    all_history = history_result.scalars().all()

    # Группируем историю в памяти: user_id -> [Reading1, Reading2, ...]
    # Ограничиваемся 3 последними записями для расчета среднего
    history_map = defaultdict(list)
    for reading in all_history:
        if len(history_map[reading.user_id]) < 3:
            history_map[reading.user_id].append(reading)

    zero = Decimal("0.000")
    zero_money = Decimal("0.00")

    # Список для массовой вставки (Bulk Insert)
    insert_values = []

    generated_count = 0

    # 5. Главный цикл расчета (в памяти Python)
    for user in all_users:
        # Если пользователь уже сдал показания (или есть черновик), пропускаем
        if user.id in users_with_readings:
            continue

        history = history_map.get(user.id, [])

        # --- Логика авто-расчета по среднему ---
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
            # Если истории нет вообще, ставим 0
            new_hot = zero
            new_cold = zero
            new_elect = zero

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
            user=user,
            tariff=tariff,
            volume_hot=vol_hot,
            volume_cold=vol_cold,
            volume_sewage=vol_hot + vol_cold,
            volume_electricity_share=max(zero, share_kwh)
        )

        # Разделение счетов (209 и 205)
        cost_rent_205 = costs['cost_social_rent']
        cost_utils_209 = costs['total_cost'] - cost_rent_205

        # Подготовка словаря для вставки
        # ВАЖНО: При использовании Core insert default значения модели могут не сработать,
        # поэтому явно указываем 0.00 для полей долгов
        row = {
            "user_id": user.id,
            "period_id": active_period.id,
            "hot_water": new_hot,
            "cold_water": new_cold,
            "electricity": new_elect,

            # Долги при автогенерации считаем нулевыми (они подгружаются из 1С отдельно)
            "debt_209": zero_money,
            "overpayment_209": zero_money,
            "debt_205": zero_money,
            "overpayment_205": zero_money,

            # Итоговые суммы
            "total_209": cost_utils_209,
            "total_205": cost_rent_205,

            "is_approved": True,
            "anomaly_flags": "AUTO_GENERATED",
            "created_at": datetime.utcnow(),

            # Разворачиваем словарь costs (cost_hot_water, cost_cold_water и т.д.)
            **costs
        }

        insert_values.append(row)
        generated_count += 1

    # 6. MASSIVE INSERT (БЫСТРАЯ ВСТАВКА)
    if insert_values:
        # Разбиваем на пачки по 1000 записей (Batching)
        chunk_size = 1000
        for i in range(0, len(insert_values), chunk_size):
            chunk = insert_values[i: i + chunk_size]
            await db.execute(insert(MeterReading), chunk)
            logger.info(f"Inserted chunk {i} to {i + len(chunk)}")

    # 7. MASSIVE UPDATE (БЫСТРОЕ ОБНОВЛЕНИЕ ЧЕРНОВИКОВ)
    # Утверждаем все черновики одним SQL запросом
    await db.execute(
        update(MeterReading)
        .where(
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(False)
        )
        .values(is_approved=True)
    )

    # 8. Закрываем период
    active_period.is_active = False

    # В роутере (admin_periods.py) вызовет commit()

    logger.info(
        f"Period '{active_period.name}' prepared for closing. "
        f"Auto-generated (Bulk): {generated_count}"
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