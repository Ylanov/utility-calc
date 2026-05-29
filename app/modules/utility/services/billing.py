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
from app.modules.utility.services.period_helpers import period_chron_key

logger = logging.getLogger("billing_service")


# =====================================================================
# Хелперы AUTO-стратегий
# =====================================================================

# _avg_monthly_delta_from_manual_history удалён в рефакторе 28.05.2026 (Коммит 2).
# Раньше использовался для стратегии AUTO_AVG — расчёт средней месячной
# дельты по manual-подачам, делённой на |Δperiod_id|. Это допущение
# «period_id ≈ календарный месяц» сломалось когда админ создавал
# ретроактивные периоды (Калачёв: |Май(88) − Начальный(1)| = 87,
# avg = 13/87 ≈ 0.149 м³ вместо нормативных 3 м³ ГВС × 4 чел = 12).
# Теперь всегда NORM-only — см. _growing_norm_volumes ниже.


# Порог санкции: после N подряд пропусков (miss_count >= NORM_SANCTION_THRESHOLD)
# норматив умножается на коэффициент. Раньше использовалось значение «3»
# inline в обеих функциях — теперь как константа модуля.
NORM_SANCTION_THRESHOLD = 3


def _growing_norm_volumes(
    user_tariff,
    residents: Decimal,
    miss_count: int,
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Норматив × пороговый коэффициент. БЕЗ умножения на residents.

    Возвращает (vol_hot, vol_cold, vol_elect, effective_coef).

    История эволюции:
      v1 (до мая 2026): 0 первые 3 месяца (AUTO_NO_HISTORY / AUTO_AVG_FALLBACK
        повторяли последние значения), потом резкая санкция × коэффициент.
      v2 (mid-may 2026): линейно растущий коэф (×1, ×2, ×3, cap).
      v3 (28.05.2026 утро): ПОРОГОВЫЙ + норматив × residents (per-capita по ПП №354).
      v4 (28.05.2026 вечер, текущая): ПОРОГОВЫЙ, БЕЗ residents — норматив
        применяется как «м³ на квартиру в месяц», независимо от числа жильцов.
        Юзер захотел простую местную логику: 3 м³ ГВС из тарифа = 3 м³ на
        всю квартиру, и точка. Не стандарт ПП №354, но устраивает юзера.

    Поведение по miss_count:
      * miss_count < NORM_SANCTION_THRESHOLD (0..2 пропусков подряд) → ×1
      * miss_count >= NORM_SANCTION_THRESHOLD (3+ подряд) → ×sanction_coefficient

    Параметр `residents` оставлен для backward-compat сигнатуры (caller'ы
    его передают), но не используется. Если когда-нибудь захотим вернуть
    per-capita по ПП №354 — добавим обратно `* residents`.
    """
    _ = residents  # явно отмечаем что параметр не используется (см. v4)
    cap = D(getattr(user_tariff, "norm_coefficient", 0) or 3)
    effective = cap if miss_count >= NORM_SANCTION_THRESHOLD else D(1)
    vol_hot = D(user_tariff.hw_norm_per_capita or 0) * effective
    vol_cold = D(user_tariff.cw_norm_per_capita or 0) * effective
    vol_el = D(user_tariff.el_norm_per_capita or 0) * effective
    return vol_hot, vol_cold, vol_el, effective


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
                ("AUTO_GENERATED", "AUTO_AVG", "AUTO_NORM", "AUTO_NORM_SANCTION",
                 "AUTO_AVG_FALLBACK", "AUTO_NO_HISTORY", "BASELINE")
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

            # NORM-only логика (28.05.2026 рефактор):
            # Раньше было 3 ветки — AUTO_NORM_SANCTION (miss>=3),
            # AUTO_AVG (если >=2 manual подач), AUTO_AVG_FALLBACK/
            # AUTO_NO_HISTORY (1 или 0 manual). AUTO_AVG считал
            # «среднюю дельту по manual history» — но при ретроактивных
            # подачах period_id не отражал биллинговую хронологию, и
            # «среднее» получало мусорные значения (Калачёв: 13/87 ≈ 0.149
            # м³ ГВС вместо нормативных 3-12). См. инцидент 28.05.2026.
            #
            # Сейчас одна формула: норматив × residents × коэф (×1 первые
            # NORM_SANCTION_THRESHOLD пропусков, ×sanction_coefficient после).
            # См. _growing_norm_volumes для деталей.
            residents = D(user.residents_count or 1)
            last_hot = D(history[0].hot_water) if history else zero
            last_cold = D(history[0].cold_water) if history else zero
            last_elect = D(history[0].electricity) if history else zero

            vol_hot, vol_cold, delta_elect, _coef = _growing_norm_volumes(
                user_tariff, residents, miss_count,
            )
            new_hot = last_hot + vol_hot
            new_cold = last_cold + vol_cold
            new_elect = last_elect + delta_elect
            anomaly_flag = (
                "AUTO_NORM_SANCTION"
                if miss_count >= NORM_SANCTION_THRESHOLD
                else "AUTO_NORM"
            )

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


async def auto_fill_period_readings(
    db: AsyncSession,
    period_id: int,
    dry_run: bool = False,
) -> dict:
    """Применить NORM-only авто-генерацию reading'ов к УКАЗАННОМУ периоду.

    Используется когда админ видит «пустой» исторический месяц (жилец
    не подал, система ничего не начислила — например свежесозданный
    ретроактивный период «Март 2026») и хочет добить его по нормативу.
    Та же стратегия что в close_current_period (NORM-only, 28.05.2026):

      * miss_count < NORM_SANCTION_THRESHOLD (0..2 пропусков подряд)
        → AUTO_NORM (норматив × residents × 1).
      * miss_count >= NORM_SANCTION_THRESHOLD (3+ подряд)
        → AUTO_NORM_SANCTION (норматив × residents × коэф санкции).

    Жильцы, у кого УЖЕ есть approved reading в этом периоде, пропускаются.

    Returns dict с stats: {processed, created, skipped_has_reading,
    by_strategy: {AUTO_NORM: N, AUTO_NORM_SANCTION: M}, dry_run}.
    """
    target_period = await db.get(BillingPeriod, period_id)
    if not target_period:
        raise ValueError(f"Период id={period_id} не найден")

    # Защита: «Начальный период» — это baseline (исходные показания счётчика
    # до первого биллингового месяца), а не месяц для авто-генерации. Если
    # admin запустит auto_fill для Начального — система увидит ВСЕ остальные
    # месяцы как «прошлые auto» (по биллинговой хронологии Начальный=(0,0)
    # самый ранний), применит санкцию × коэф и засрёт baseline лишними
    # начислениями. Случилось 29.05.2026 с Нежведиловым:
    # Начальный auto-сгенерировался как AUTO_NORM_SANCTION 27/63/900,
    # стало 4 «прошлых auto» в истории → sanction × 3 от показаний Мая.
    if period_chron_key(target_period.name) == (0, 0):
        raise ValueError(
            f"Период id={period_id} ('{target_period.name}') — baseline, "
            "не предназначен для auto-генерации. Reading'и за «Начальный "
            "период» создаются только через GSheets-импорт (INITIAL_FROM_GSHEETS) "
            "или ручной ввод (admin)."
        )

    tariffs_result = await db.execute(select(Tariff).where(Tariff.is_active))
    active_tariffs = tariffs_result.scalars().all()
    if not active_tariffs:
        raise ValueError("Нет активных тарифов")
    default_tariff = next((t for t in active_tariffs if t.id == 1), active_tariffs[0])

    # Жильцы, у кого УЖЕ есть reading в этом периоде (любой статус) — пропуск.
    existing_user_ids = set((await db.execute(
        select(MeterReading.user_id).where(
            MeterReading.period_id == target_period.id,
            MeterReading.user_id.is_not(None),
        )
    )).scalars().all())

    users_to_process = (await db.execute(
        select(User)
        .options(selectinload(User.room))
        .where(
            User.role == "user",
            User.is_deleted.is_(False),
            User.room_id.is_not(None),
        )
    )).scalars().all()
    users_to_process = [u for u in users_to_process if u.id not in existing_user_ids]

    if not users_to_process:
        return {
            "status": "ok", "period_id": target_period.id,
            "period_name": target_period.name,
            "processed": 0, "created": 0, "skipped_has_reading": len(existing_user_ids),
            "by_strategy": {}, "dry_run": dry_run,
        }

    from app.modules.utility.routers.settings import _load_seasonal
    _seasonal = await _load_seasonal(db)

    zero = Decimal("0.000")
    zero_money = Decimal("0.00")
    by_strategy = defaultdict(int)
    insert_values = []
    preview = []  # для dry_run

    def _is_auto(reading) -> bool:
        flags = (reading.anomaly_flags or "").upper()
        return any(t in flags for t in
                   ("AUTO_GENERATED", "AUTO_AVG", "AUTO_NORM", "AUTO_NORM_SANCTION",
                 "AUTO_AVG_FALLBACK", "AUTO_NO_HISTORY", "BASELINE"))

    for user in users_to_process:
        from app.modules.utility.services.tariff_cache import tariff_cache
        user_tariff = (
            tariff_cache.get_effective_tariff(user=user, room=user.room)
            or default_tariff
        )
        # История по этому жильцу: 6 последних approved до target_period.
        # Берём по user_id, не room_id (Bug AG: долги/показания per-user).
        #
        # Сортируем по БИЛЛИНГОВОЙ ХРОНОЛОГИИ (period_chron_key из BillingPeriod
        # .name), а не по period_id. period_id ≠ хронология когда админ создаёт
        # ретроактивные периоды — например «Февраль 2026» с id=90 после «Май
        # 2026» с id=88. См. длинный комментарий в skip_recalc.py:118-130.
        # Возвращаем DESC (свежие первые) — miss_count loop ниже считает
        # подряд auto'ы от свежего к старому.
        rows = (await db.execute(
            select(MeterReading, BillingPeriod)
            .join(BillingPeriod, MeterReading.period_id == BillingPeriod.id)
            .where(
                MeterReading.user_id == user.id,
                MeterReading.is_approved.is_(True),
                MeterReading.period_id != target_period.id,
            )
        )).all()
        # КРИТИЧНО: фильтруем history так чтобы оставались ТОЛЬКО периоды
        # ХРОНОЛОГИЧЕСКИ РАНЬШЕ target. Иначе для ранних месяцев (например
        # auto_fill для Февраль) система брала бы Май как «предыдущий» —
        # ведь по выборке history содержит ВСЕ approved reading'и.
        # Случай Капранова (29.05.2026): target=Февраль, history=[Май,
        # Начальный]. По chron DESC Май(2026,5) > Начальный(0,0), и
        # last_hot = history[0] = Май = 1468 → Февраль hot_water стал
        # 1468 + 3 = 1471 (вместо правильного 1456 + 3 = 1459 от
        # Начального). Внутренний consistency сломан.
        # Fix: оставляем только rows с chron < target_chron.
        target_chron = period_chron_key(target_period.name)
        rows = [(r, p) for r, p in rows if period_chron_key(p.name) < target_chron]
        history = [r for r, _p in sorted(
            rows, key=lambda row: period_chron_key(row[1].name), reverse=True
        )][:6]

        miss_count = 0
        for r in history:
            if _is_auto(r):
                miss_count += 1
            else:
                break

        # NORM-only логика (28.05.2026 рефактор). Одна формула:
        # норматив × residents × (1 если miss<3, иначе sanction_coefficient).
        # Подробности и история эволюции — в close_current_period выше и
        # docstring _growing_norm_volumes.
        residents = D(user.residents_count or 1)
        last_hot = D(history[0].hot_water) if history else zero
        last_cold = D(history[0].cold_water) if history else zero
        last_elect = D(history[0].electricity) if history else zero

        vol_hot, vol_cold, delta_elect, _coef = _growing_norm_volumes(
            user_tariff, residents, miss_count,
        )
        new_hot = last_hot + vol_hot
        new_cold = last_cold + vol_cold
        new_elect = last_elect + delta_elect
        anomaly_flag = (
            "AUTO_NORM_SANCTION"
            if miss_count >= NORM_SANCTION_THRESHOLD
            else "AUTO_NORM"
        )

        total_residents = D(
            user.room.total_room_residents
            if user.room and user.room.total_room_residents > 0 else 1
        )
        share_kwh = max(zero, (residents / total_residents) * delta_elect)

        _heating = _seasonal.heating_season_active and user_tariff.is_heating_active_now()
        _hw = _seasonal.hot_water_heating_active and user_tariff.is_hw_heating_active_now()
        costs = calculate_utilities(
            user=user, room=user.room, tariff=user_tariff,
            volume_hot=vol_hot, volume_cold=vol_cold,
            volume_sewage=vol_hot + vol_cold, volume_electricity_share=share_kwh,
            heating_season_active=_heating,
            hot_water_heating_active=_hw,
        )
        cost_205 = costs['cost_social_rent']
        cost_209 = costs['total_cost'] - cost_205

        by_strategy[anomaly_flag] += 1

        if dry_run:
            preview.append({
                "user_id": user.id, "username": user.username,
                "strategy": anomaly_flag,
                "vol_hot": float(vol_hot), "vol_cold": float(vol_cold),
                "vol_elect": float(delta_elect),
                "total_cost": float(costs['total_cost']),
            })
            continue

        insert_values.append({
            "user_id": user.id, "room_id": user.room_id, "period_id": target_period.id,
            "hot_water": new_hot, "cold_water": new_cold, "electricity": new_elect,
            "debt_209": zero_money, "overpayment_209": zero_money,
            "debt_205": zero_money, "overpayment_205": zero_money,
            "total_209": cost_209, "total_205": cost_205,
            "is_approved": True, "anomaly_flags": anomaly_flag, "anomaly_score": 0,
            "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
            **costs,
        })

    created = 0
    if insert_values and not dry_run:
        await db.execute(insert(MeterReading), insert_values)
        created = len(insert_values)
        await db.commit()
        logger.info(
            "[AUTO-FILL] period=%s created=%d by_strategy=%s",
            target_period.name, created, dict(by_strategy),
        )

    return {
        "status": "ok",
        "period_id": target_period.id,
        "period_name": target_period.name,
        "processed": len(users_to_process),
        "created": created if not dry_run else 0,
        "would_create": len(preview) if dry_run else None,
        "skipped_has_reading": len(existing_user_ids),
        "by_strategy": dict(by_strategy),
        "preview": preview[:50] if dry_run else None,
        "dry_run": dry_run,
    }


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
