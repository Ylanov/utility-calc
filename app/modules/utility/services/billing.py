# app/modules/utility/services/billing.py

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update, insert, func
from sqlalchemy.orm import selectinload

from datetime import datetime, timezone
from decimal import Decimal
import logging
from collections import defaultdict

from app.modules.utility.models import User, MeterReading, BillingPeriod, Tariff
from app.modules.utility.services.calculations import calculate_utilities, D

logger = logging.getLogger("billing_service")

async def close_current_period(db: AsyncSession, admin_user_id: int):
    """
    Закрывает текущий расчетный период с генерацией недостающих показаний.
    ОПТИМИЗИРОВАНО: Использован батчинг (chunking) для защиты от OOM (Out Of Memory).
    """

    # 1. Блокируем запись активного периода (здесь это безопасно, так как строка одна)
    result = await db.execute(
        select(BillingPeriod)
        .where(BillingPeriod.is_active.is_(True))
        .with_for_update()
    )
    active_period = result.scalars().first()

    if not active_period or not active_period.is_active:
        raise ValueError("Нет активного периода для закрытия или он уже закрыт.")

    # 2. Получаем тарифы (они нужны в памяти, их мало)
    tariffs_result = await db.execute(select(Tariff).where(Tariff.is_active))
    active_tariffs = tariffs_result.scalars().all()
    if not active_tariffs:
        raise ValueError("В системе нет активных тарифов.")

    tariffs_map = {t.id: t for t in active_tariffs}
    default_tariff = tariffs_map.get(1) or active_tariffs[0]

    # 3. Комнаты с показаниями (исключаем их из авто-генерации)
    submitted_readings_res = await db.execute(
        select(MeterReading.room_id).where(MeterReading.period_id == active_period.id)
    )
    rooms_with_readings = set(submitted_readings_res.scalars().all())

    room_filter = User.room_id.notin_(rooms_with_readings) if rooms_with_readings else True

    # 4. Загружаем пользователей для авторасчета
    users_to_process_res = await db.execute(
        select(User)
        .options(selectinload(User.room))
        .where(
            User.role == "user",
            User.is_deleted.is_(False),
            User.room_id.is_not(None),
            room_filter
        )
    )
    all_users_to_process = users_to_process_res.scalars().all()

    # Оставляем только одного представителя на комнату (чтобы не генерить 2 счета на одну комнату)
    unique_rooms_map = {}
    for u in all_users_to_process:
        if u.room_id not in unique_rooms_map:
            unique_rooms_map[u.room_id] = u

    users_to_process = list(unique_rooms_map.values())

    if not users_to_process:
        # Нечего генерировать, просто утверждаем черновики и закрываем
        active_period.is_active = False
        await db.execute(
            update(MeterReading)
            .where(MeterReading.period_id == active_period.id, MeterReading.is_approved.is_(False))
            .values(is_approved=True)
        )
        return {"status": "closed", "closed_period": active_period.name, "auto_generated": 0}

    zero = Decimal("0.000")
    zero_money = Decimal("0.00")
    generated_count = 0
    chunk_size = 500  # ИСПРАВЛЕНИЕ: Разбиваем на пачки по 500 комнат

    # 5. ОБРАБОТКА БАТЧАМИ (Защита RAM)
    for i in range(0, len(users_to_process), chunk_size):
        chunk_users = users_to_process[i:i + chunk_size]
        chunk_room_ids = [u.room_id for u in chunk_users]

        # Запрашиваем историю только для текущего чанка!
        ranked_readings_subquery = (
            select(
                MeterReading,
                func.row_number().over(
                    partition_by=MeterReading.room_id,
                    order_by=MeterReading.created_at.desc()
                ).label("row_num")
            )
            .where(
                MeterReading.room_id.in_(chunk_room_ids),
                MeterReading.is_approved.is_(True)
            )
            .subquery()
        )

        recent_history_result = await db.execute(
            select(ranked_readings_subquery).where(ranked_readings_subquery.c.row_num <= 4)
        )

        history_map = defaultdict(list)
        for row in recent_history_result.all():
            reading_obj = MeterReading(**{c.name: getattr(row, c.name) for c in MeterReading.__table__.columns})
            history_map[getattr(row, "room_id")].append(reading_obj)

        insert_values = []

        # Расчет внутри чанка
        for user in chunk_users:
            user_tariff = tariffs_map.get(getattr(user, "tariff_id", None)) or default_tariff
            history = history_map.get(user.room_id, [])
            history.sort(key=lambda r: r.created_at, reverse=True)

            if len(history) >= 2:
                d_hot, d_cold, d_el = [], [], []
                for j in range(len(history) - 1):
                    curr, prev = history[j], history[j + 1]
                    d_hot.append(max(zero, D(curr.hot_water) - D(prev.hot_water)))
                    d_cold.append(max(zero, D(curr.cold_water) - D(prev.cold_water)))
                    d_el.append(max(zero, D(curr.electricity) - D(prev.electricity)))

                cnt = len(d_hot)
                new_hot = D(history[0].hot_water) + (sum(d_hot) / cnt)
                new_cold = D(history[0].cold_water) + (sum(d_cold) / cnt)
                new_elect = D(history[0].electricity) + (sum(d_el) / cnt)
            elif len(history) == 1:
                new_hot, new_cold, new_elect = D(history[0].hot_water), D(history[0].cold_water), D(history[0].electricity)
            else:
                new_hot, new_cold, new_elect = zero, zero, zero

            last_hot = D(history[0].hot_water) if history else zero
            last_cold = D(history[0].cold_water) if history else zero
            last_elect = D(history[0].electricity) if history else zero

            vol_hot = max(zero, new_hot - last_hot)
            vol_cold = max(zero, new_cold - last_cold)
            delta_elect = new_elect - last_elect

            residents = D(user.residents_count)
            total_residents = D(user.room.total_room_residents if user.room and user.room.total_room_residents > 0 else 1)
            share_kwh = (residents / total_residents) * delta_elect

            costs = calculate_utilities(
                user=user, room=user.room, tariff=user_tariff,
                volume_hot=vol_hot, volume_cold=vol_cold,
                volume_sewage=vol_hot + vol_cold, volume_electricity_share=max(zero, share_kwh)
            )

            cost_rent_205 = costs['cost_social_rent']
            cost_utils_209 = costs['total_cost'] - cost_rent_205

            insert_values.append({
                "user_id": user.id, "room_id": user.room_id, "period_id": active_period.id,
                "hot_water": new_hot, "cold_water": new_cold, "electricity": new_elect,
                "debt_209": zero_money, "overpayment_209": zero_money,
                "debt_205": zero_money, "overpayment_205": zero_money,
                "total_209": cost_utils_209, "total_205": cost_rent_205,
                "is_approved": True, "anomaly_flags": "AUTO_GENERATED", "anomaly_score": 0,
                "created_at": datetime.now(timezone.utc).replace(tzinfo=None),  # ИСПРАВЛЕНИЕ
                **costs
            })
            generated_count += 1

        # Сбрасываем чанк в базу (используем bulk insert)
        if insert_values:
            await db.execute(insert(MeterReading), insert_values)

    # 6. Утверждаем оставшиеся черновики
    await db.execute(
        update(MeterReading)
        .where(MeterReading.period_id == active_period.id, MeterReading.is_approved.is_(False))
        .values(is_approved=True)
    )

    # 7. Закрываем период
    active_period.is_active = False
    logger.info(f"Period '{active_period.name}' closed. Auto-generated: {generated_count}")

    return {"status": "closed", "closed_period": active_period.name, "auto_generated": generated_count}


async def open_new_period(db: AsyncSession, new_name: str):
    active_result = await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True)).with_for_update()
    )

    if active_result.scalars().first():
        raise ValueError("Сначала закройте текущий активный месяц.")

    exist_result = await db.execute(select(BillingPeriod).where(BillingPeriod.name == new_name))
    if exist_result.scalars().first():
        raise ValueError(f"Период '{new_name}' уже существует.")

    new_period = BillingPeriod(
        name=new_name,
        is_active=True,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None)  # ИСПРАВЛЕНИЕ
    )

    db.add(new_period)
    await db.flush()
    await db.refresh(new_period)
    return new_period