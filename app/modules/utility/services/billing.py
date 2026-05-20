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

    # Сезонные флаги читаем ОДИН раз перед всеми чанками (это batch-генерация
    # для невозвратчиков при закрытии периода — может быть несколько тысяч
    # жильцов, без кеширования +N SELECT'ов).
    from app.modules.utility.routers.settings import _load_seasonal
    _seasonal = await _load_seasonal(db)

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

        # row_num <= 6 — последние 6 reading'ов, нужны для:
        # 1) подсчёта miss_count (сколько подряд AUTO_GENERATED / AUTO_AVG / AUTO_NORM)
        # 2) среднего по 3-4 manual-подачам
        recent_history_result = await db.execute(
            select(ranked_readings_subquery).where(ranked_readings_subquery.c.row_num <= 6)
        )

        history_map = defaultdict(list)
        for row in recent_history_result.all():
            reading_obj = MeterReading(**{c.name: getattr(row, c.name) for c in MeterReading.__table__.columns})
            history_map[getattr(row, "room_id")].append(reading_obj)

        insert_values = []

        def _is_auto(reading) -> bool:
            """True если reading создан автоматически (а не подан вручную)."""
            flags = (reading.anomaly_flags or "").upper()
            return any(
                t in flags for t in
                ("AUTO_GENERATED", "AUTO_AVG", "AUTO_NORM_SANCTION", "BASELINE")
            )

        # Расчет внутри чанка
        for user in chunk_users:
            # Через единый кеш + приоритет Room.tariff_id → User.tariff_id → default.
            from app.modules.utility.services.tariff_cache import tariff_cache
            user_tariff = (
                tariff_cache.get_effective_tariff(user=user, room=getattr(user, "room", None))
                or default_tariff
            )
            history = history_map.get(user.room_id, [])
            history.sort(key=lambda r: r.created_at, reverse=True)

            # Сколько последних периодов подряд reading был AUTO (не вручную).
            miss_count = 0
            for r in history:
                if _is_auto(r):
                    miss_count += 1
                else:
                    break

            # Manual history (для расчёта среднего «нормально подавал»).
            manual_history = [r for r in history if not _is_auto(r)]

            # Решение какую стратегию применить.
            # Порог санкции: 3 подряд AUTO → следующий (текущий) уже 4-й → норматив × коэф.
            # До этого — берём среднее по manual_history (если есть) или
            # baseline+norm (если у жильца вообще нет manual-подач).
            sanction_threshold = 3
            apply_sanction = miss_count >= sanction_threshold
            residents = D(user.residents_count or 1)

            anomaly_flag = "AUTO_GENERATED"
            new_hot, new_cold, new_elect = zero, zero, zero
            vol_hot = vol_cold = delta_elect = zero
            last_hot = D(history[0].hot_water) if history else zero
            last_cold = D(history[0].cold_water) if history else zero
            last_elect = D(history[0].electricity) if history else zero

            if apply_sanction:
                # Санкция: норматив × жильцов × коэффициент. Накопленное
                # значение += санкционное потребление.
                coef = D(getattr(user_tariff, "norm_coefficient", 0) or 3)
                vol_hot = D(user_tariff.hw_norm_per_capita or 0) * residents * coef
                vol_cold = D(user_tariff.cw_norm_per_capita or 0) * residents * coef
                delta_elect = D(user_tariff.el_norm_per_capita or 0) * residents * coef
                new_hot = last_hot + vol_hot
                new_cold = last_cold + vol_cold
                new_elect = last_elect + delta_elect
                anomaly_flag = "AUTO_NORM_SANCTION"
            elif len(manual_history) >= 2:
                # Среднее по дельтам между подряд идущими manual-readings.
                d_hot, d_cold, d_el = [], [], []
                # manual_history отсортирован desc по created_at; берём пары соседей.
                for j in range(len(manual_history) - 1):
                    curr, prev = manual_history[j], manual_history[j + 1]
                    d_hot.append(max(zero, D(curr.hot_water) - D(prev.hot_water)))
                    d_cold.append(max(zero, D(curr.cold_water) - D(prev.cold_water)))
                    d_el.append(max(zero, D(curr.electricity) - D(prev.electricity)))
                cnt = D(len(d_hot)) if d_hot else D(1)
                avg_hot = sum(d_hot, zero) / cnt
                avg_cold = sum(d_cold, zero) / cnt
                avg_el = sum(d_el, zero) / cnt
                vol_hot, vol_cold, delta_elect = avg_hot, avg_cold, avg_el
                new_hot = last_hot + avg_hot
                new_cold = last_cold + avg_cold
                new_elect = last_elect + avg_el
                anomaly_flag = "AUTO_AVG"
            elif len(manual_history) == 1:
                # Одна подача — не от чего считать дельту, используем баранье
                # «то же значение, что и в прошлый раз» (расход 0). Так было
                # и в старом коде.
                last = manual_history[0]
                new_hot, new_cold, new_elect = D(last.hot_water), D(last.cold_water), D(last.electricity)
                anomaly_flag = "AUTO_AVG_FALLBACK"
            else:
                # Нет manual-истории совсем. На данной итерации — оставляем zero
                # (только фикс-часть начислится). В следующий релиз можно
                # добавить «средний расход по общежитию» через отдельный SQL.
                # Тут же оптимизировать не критично — это редкий кейс (только
                # новые жильцы которые ни разу не подавали).
                anomaly_flag = "AUTO_NO_HISTORY"

            total_residents = D(
                user.room.total_room_residents
                if user.room and user.room.total_room_residents > 0 else 1
            )
            share_kwh = max(zero, (residents / total_residents) * delta_elect)

            _heating = (
                _seasonal.heating_season_active
                and user_tariff.is_heating_active_now()
            )
            _hw = (
                _seasonal.hot_water_heating_active
                and user_tariff.is_hw_heating_active_now()
            )
            costs = calculate_utilities(
                user=user, room=user.room, tariff=user_tariff,
                volume_hot=vol_hot, volume_cold=vol_cold,
                volume_sewage=vol_hot + vol_cold, volume_electricity_share=share_kwh,
                heating_season_active=_heating,
                hot_water_heating_active=_hw,
            )

            cost_rent_205 = costs['cost_social_rent']
            cost_utils_209 = costs['total_cost'] - cost_rent_205

            insert_values.append({
                "user_id": user.id, "room_id": user.room_id, "period_id": active_period.id,
                "hot_water": new_hot, "cold_water": new_cold, "electricity": new_elect,
                "debt_209": zero_money, "overpayment_209": zero_money,
                "debt_205": zero_money, "overpayment_205": zero_money,
                "total_209": cost_utils_209, "total_205": cost_rent_205,
                "is_approved": True, "anomaly_flags": anomaly_flag, "anomaly_score": 0,
                "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
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
