# app/modules/utility/services/admin_readings_manual.py
import logging
from decimal import Decimal
from typing import Optional
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment
from app.modules.utility.schemas import AdminManualReadingSchema, OneTimeChargeSchema
from app.modules.utility.services.calculations import (
    calculate_utilities,
    costs_for_model_fields,
    paying_residents,
    MODEL_COST_FIELDS,
)
from app.modules.utility.services.anomaly_detector import check_reading_for_anomalies_v2

ZERO = Decimal("0.00")


async def _recompute_real_chain(db: AsyncSession, user, room, tariff) -> int:
    """Пересчитать всю цепочку РЕАЛЬНЫХ (не AUTO) approved-показаний жильца в
    этой комнате по биллинговой хронологии: каждое — дельта от предыдущего
    осмысленного. Нужно после доввода показаний за ПРОШЛЫЙ месяц задним числом
    — тогда следующий месяц (напр. май) подхватывает новое prev (апрель) и его
    суммы сразу пересчитываются. Возвращает число пересчитанных показаний.

    Использует канонический compute_reading_breakdown (тот же калькулятор, что и
    gsheets-promote). AUTO-показания (норматив/авто) НЕ трогаем и в prev не берём
    (is_meaningful_prev=False) — они оценочные."""
    from app.modules.utility.services.reading_calculator import (
        compute_reading_breakdown, is_meaningful_prev,
    )
    from app.modules.utility.services.period_helpers import period_chron_key
    from app.modules.utility.routers.settings import _load_seasonal

    rows = (await db.execute(
        select(MeterReading, BillingPeriod)
        .join(BillingPeriod, MeterReading.period_id == BillingPeriod.id)
        .where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved.is_(True),
        )
    )).all()
    if not rows:
        return 0
    chain = [r for r, _p in sorted(rows, key=lambda rp: period_chron_key(rp[1].name))]

    # Корректировки 209/205 по периодам — одним запросом (сохраняем в суммах).
    adj_rows = (await db.execute(
        select(Adjustment.period_id, Adjustment.account_type, func.sum(Adjustment.amount))
        .where(Adjustment.user_id == user.id)
        .group_by(Adjustment.period_id, Adjustment.account_type)
    )).all()
    adj_by_period: dict = {}
    for pid, acc, amount in adj_rows:
        adj_by_period.setdefault(pid, {})[acc] = amount or ZERO

    seasonal = await _load_seasonal(db)
    heating = seasonal.heating_season_active and tariff.is_heating_active_now()
    hw = seasonal.hot_water_heating_active and tariff.is_hw_heating_active_now()

    prev_meaningful = None
    changed = 0
    for r in chain:
        # AUTO/synth-показания не пересчитываем и в prev не берём.
        if not is_meaningful_prev(r):
            continue
        try:
            bd = compute_reading_breakdown(
                user=user, room=room, tariff=tariff,
                current_hot=r.hot_water, current_cold=r.cold_water,
                current_elect=r.electricity, prev_reading=prev_meaningful,
                heating_season_active=heating, hot_water_heating_active=hw,
            )
        except Exception:
            prev_meaningful = r
            continue
        adj = adj_by_period.get(r.period_id, {})
        t209 = bd["total_209"] + (adj.get("209") or ZERO)
        t205 = bd["total_205"] + (adj.get("205") or ZERO)
        for k, v in costs_for_model_fields(bd).items():
            setattr(r, k, v)
        r.total_209, r.total_205, r.total_cost = t209, t205, t209 + t205
        if bd.get("is_baseline"):
            r.anomaly_flags, r.anomaly_score = "BASELINE", 0
        elif (r.anomaly_flags or "") == "BASELINE":
            # Больше не первый: появилось prev (доввод за прошлый месяц) —
            # снимаем BASELINE, теперь это нормальная подача с расходом.
            r.anomaly_flags, r.anomaly_score = "", 0
        db.add(r)
        prev_meaningful = r
        changed += 1
    return changed


async def recalc_user_period(db: AsyncSession, *, user_id: int, period_id: int) -> dict:
    """Перерасчёт ОДНОГО жильца за ОДИН период по текущему тарифу/состоянию.
    Работает для ЛЮБОГО периода (открытый/закрытый — не важно, без проверки
    is_active). Холостяцкая квартира → выравнивание поровну (equalize); семья →
    прямой пересчёт его показания. Сальдо 1С (debt/overpayment) не трогаем."""
    from app.modules.utility.services.reading_calculator import (
        compute_reading_breakdown, is_meaningful_prev,
    )
    from app.modules.utility.services.period_helpers import period_chron_key
    from app.modules.utility.routers.settings import _load_seasonal
    from app.modules.utility.services.tariff_cache import tariff_cache
    from app.modules.utility.services.singles_billing import (
        equalize_singles_room, propagate_singles_reading,
    )
    from app.modules.utility.services.room_assignment import recount_singles_residents

    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == user_id)
    )).scalars().first()
    if not user or user.is_deleted:
        raise HTTPException(404, "Жилец не найден")
    room = user.room
    if not room:
        raise HTTPException(400, "Жилец не привязан к помещению")
    period = await db.get(BillingPeriod, period_id)
    if period is None:
        raise HTTPException(404, "Период не найден")

    tariff = tariff_cache.get_effective_tariff(user=user, room=room) or \
        (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()
    if tariff is None:
        raise HTTPException(400, "Нет активного тарифа")

    is_singles = bool(getattr(room, "is_singles_apartment", False))

    # Холостяцкая квартира: equalize пересчитывает источник (макс. счётчик) по
    # текущему тарифу и раскидывает РАВНУЮ долю на всех. Делитель освежаем.
    if is_singles:
        await recount_singles_residents(db, room.id)
        await db.flush()
        res = await equalize_singles_room(db, room=room, period_id=period_id)
        if res.get("status") == "equalized":
            await db.commit()
            s209 = res.get("share_209") or 0
            s205 = res.get("share_205") or 0
            return {"status": "ok", "singles": True,
                    "total_209": float(s209), "total_205": float(s205),
                    "total_cost": float(s209 + s205), "detail": res}
        # equalize пропустил (черновик / <2 жильцов) → прямой пересчёт ниже.

    # Прямой пересчёт показания ЭТОГО жильца за период (черновик ИЛИ approved).
    reading = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.period_id == period_id,
        ).order_by(MeterReading.is_approved.desc(), MeterReading.id.desc()).limit(1)
    )).scalars().first()
    if reading is None:
        raise HTTPException(404, "Нет показания этого жильца за выбранный период")

    # prev — последнее ОСМЫСЛЕННОЕ approved-показание ХРОНОЛОГИЧЕСКИ раньше.
    cur_key = period_chron_key(period.name)
    prev_rows = (await db.execute(
        select(MeterReading, BillingPeriod)
        .join(BillingPeriod, MeterReading.period_id == BillingPeriod.id)
        .where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved.is_(True),
        )
    )).all()
    earlier = [(period_chron_key(p.name), mr) for mr, p in prev_rows]
    earlier = [(k, mr) for k, mr in earlier
               if k is not None and cur_key is not None and k < cur_key and is_meaningful_prev(mr)]
    earlier.sort(key=lambda x: x[0])
    prev_reading = earlier[-1][1] if earlier else None

    seasonal = await _load_seasonal(db)
    heating = seasonal.heating_season_active and tariff.is_heating_active_now()
    hw = seasonal.hot_water_heating_active and tariff.is_hw_heating_active_now()
    bd = compute_reading_breakdown(
        user=user, room=room, tariff=tariff,
        current_hot=reading.hot_water, current_cold=reading.cold_water,
        current_elect=reading.electricity, prev_reading=prev_reading,
        heating_season_active=heating, hot_water_heating_active=hw,
    )
    adj = {row[0]: (row[1] or ZERO) for row in (await db.execute(
        select(Adjustment.account_type, func.sum(Adjustment.amount))
        .where(Adjustment.user_id == user.id, Adjustment.period_id == period_id)
        .group_by(Adjustment.account_type))).all()}
    t209 = bd["total_209"] + (adj.get("209") or ZERO)
    t205 = bd["total_205"] + (adj.get("205") or ZERO)
    for k, v in costs_for_model_fields(bd).items():
        setattr(reading, k, v)
    reading.total_209, reading.total_205, reading.total_cost = t209, t205, t209 + t205
    # Флаг BASELINE синхронизируем (как _recompute_real_chain) — чтобы в реестре
    # не висел устаревший после появления/исчезновения prev.
    if bd.get("is_baseline"):
        reading.anomaly_flags, reading.anomaly_score = "BASELINE", 0
    elif (reading.anomaly_flags or "") == "BASELINE":
        reading.anomaly_flags, reading.anomaly_score = "", 0
    db.add(reading)

    # Холостяк (fallback-путь): раскидать РАВНУЮ долю (без корректировок) на остальных.
    if is_singles:
        await db.flush()
        _costs = {f: getattr(reading, f) for f in MODEL_COST_FIELDS}
        await propagate_singles_reading(
            db, room=room, period_id=period_id, source_user_id=user.id,
            hot=reading.hot_water, cold=reading.cold_water, elect=reading.electricity,
            costs=_costs, total_209=bd["total_209"], total_205=bd["total_205"],
            flags=reading.anomaly_flags, is_approved=bool(reading.is_approved),
        )

    await db.commit()
    return {"status": "ok", "singles": is_singles,
            "total_209": float(t209), "total_205": float(t205),
            "total_cost": float(t209 + t205)}


async def recalc_building_period(
    db: AsyncSession, *, period_id: int, group: str,
) -> dict:
    """Перерасчёт ВСЕГО дома/общаги за период: пробегает активных жильцов здания
    и для каждого вызывает recalc_user_period (холостяк→equalize, семья→прямой).
    Любой период (открытый/закрытый). Здание задаётся `group` — тем же ключом,
    что _building_key (как в финотчёте и в фильтре начислений), поэтому фронт
    шлёт ровно имя из карточки дома. Возвращает {processed, errors}."""
    from app.modules.utility.services.billing import _building_key
    from app.modules.utility.models import Room as _Room

    rooms = (await db.execute(select(_Room))).scalars().all()
    matching = [r for r in rooms if _building_key(r) == group]
    room_ids = [r.id for r in matching]
    if not room_ids:
        raise HTTPException(404, "Здание не найдено")
    # Холостяцкие комнаты: equalize_singles_room (внутри recalc_user_period)
    # пересчитывает и раскидывает на ВСЕХ жильцов комнаты разом — поэтому
    # достаточно одного вызова на комнату, иначе работа (recompute+propagate+
    # commit) повторяется на каждого соседа. Считаем покрытых жильцов отдельно.
    singles_room_ids = {r.id for r in matching if getattr(r, "is_singles_apartment", False)}
    users = (await db.execute(
        select(User.id, User.room_id).where(
            User.role == "user", User.is_deleted.is_(False), User.room_id.in_(room_ids),
        )
    )).all()
    residents_per_room: dict[int, int] = {}
    for _uid, rid in users:
        residents_per_room[rid] = residents_per_room.get(rid, 0) + 1

    processed = 0
    errors: list[dict] = []
    done_singles: set[int] = set()
    for uid, rid in users:
        if rid in singles_room_ids:
            if rid in done_singles:
                continue  # комната уже выровнена этим прогоном
            done_singles.add(rid)
        try:
            await recalc_user_period(db, user_id=uid, period_id=period_id)
            # У холостяков один вызов покрывает всех жильцов комнаты.
            processed += residents_per_room.get(rid, 1) if rid in singles_room_ids else 1
        except HTTPException as e:
            # «нет показания за период» и т.п. — пропускаем, копим в errors.
            # rollback: recalc_user_period мог flush'нуть recount делителя ДО
            # raise — откатываем, чтобы частичная мутация не утекла в след. жильца.
            await db.rollback()
            errors.append({"user_id": uid, "reason": str(getattr(e, "detail", e))})
        except Exception as e:  # noqa: BLE001
            await db.rollback()
            errors.append({"user_id": uid, "reason": str(e)})
    return {"status": "ok", "processed": processed,
            "errors": errors[:20], "errors_count": len(errors)}


async def save_manual_entry(db: AsyncSession, data: AdminManualReadingSchema):
    """Сохранение черновика бухгалтером вручную.

    Раздельная подача (май 2026): админ может прислать только воду
    (ГВС+ХВС), только электричество или всё сразу. Правила:
      - ГВС и ХВС подаются ПАРОЙ — оба или ни одного.
      - Электричество — независимо.
      - Хотя бы один из счётчиков должен быть подан.
      - «Не подавал» = None → в БД пишется prev (счётчик не двигается,
        дельта 0, расход не начисляется по этому ресурсу).

    Если data.period_id задан — используем его (для ввода за прошлый
    месяц). Если None — берём текущий active_period (back-compat).
    """
    # Раздельная подача — проверки целостности.
    hot_provided = data.hot_water is not None
    cold_provided = data.cold_water is not None
    elect_provided = data.electricity is not None

    if hot_provided != cold_provided:
        raise HTTPException(
            status_code=400,
            detail="Горячая и холодная вода подаются вместе — оба значения или ни одного.",
        )
    if not (hot_provided or elect_provided):
        raise HTTPException(
            status_code=400,
            detail="Передайте хотя бы один ресурс — вода (ГВС+ХВС) или электричество.",
        )

    if data.period_id is not None:
        # Админ ввёл за конкретный период. Проверяем что такой существует.
        active_period = await db.get(BillingPeriod, data.period_id)
        if active_period is None:
            raise HTTPException(status_code=400, detail=f"Период id={data.period_id} не найден.")
    else:
        active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))).scalars().first()
        if not active_period:
            raise HTTPException(status_code=400, detail="Расчетный период закрыт.")

    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == data.user_id))).scalars().first()
    if not user or user.is_deleted: raise HTTPException(status_code=404, detail="Жилец не найден")

    room = user.room
    if not room: raise HTTPException(status_code=400, detail="Жилец не привязан к помещению")

    # housing_001/E2-B: для дома (place_type='house') счётчиков нет —
    # ручной ввод показаний бессмыслен. UI скрывает раздел подачи для
    # домовых жильцов, но дублируем на API чтобы и через curl нельзя
    # было создать MeterReading для дома.
    from app.modules.utility.services.room_validators import (
        require_room_has_meters,
    )
    require_room_has_meters(room)

    # Через единый кеш — Room.tariff_id побеждает User.tariff_id (см. tariff_cache.py).
    from app.modules.utility.services.tariff_cache import tariff_cache
    t = tariff_cache.get_effective_tariff(user=user, room=room) or \
        (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()

    # История считается ПО ЖИЛЬЦУ В ЭТОЙ КОМНАТЕ, не по комнате в целом.
    # Если до этого жильца тут были показания (старый жилец, GSHEETS_AUTO
    # и т.п.), их учитывать нельзя — получились бы миллионы.
    history = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.period))
        .where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved,
            # Ревизия #3 (решение «baseline=0»): period_id=NULL baseline
            # (INITIAL_SETUP) НЕ берём как prev — первая реальная подача = baseline
            # (расход 0), единообразно с approve_single/client/tasks/gsheets.
            MeterReading.period_id.isnot(None),
        )
        .order_by(MeterReading.created_at.desc()).limit(24)
    )).scalars().all()

    # ВЫБОР prev ПО ХРОНОЛОГИИ ПЕРИОДА, а не по дате создания (fix 2026-06-16).
    # Раньше prev = последний СОЗДАННЫЙ reading — и при вводе ЗА ПРОШЛЫЙ месяц
    # (напр. апрель, когда май уже введён) prev оказывался майским → апрель <
    # мая ловилось как «счётчик упал», а дельта считалась от мая. Теперь
    # prev = ближайшее ПРЕДЫДУЩЕЕ по биллинговому месяцу показание (для апреля
    # это март, май игнорируется). Админ может вводить за любой месяц в любую
    # сторону без ложных ошибок.
    from app.modules.utility.services.period_helpers import period_chron_key
    from app.modules.utility.services.reading_calculator import is_meaningful_prev
    _target_key = period_chron_key(active_period.name)

    def _rkey(r):
        return period_chron_key(r.period.name) if r.period else (0, 0)

    # Кандидаты строго ДО целевого месяца, по убыванию хронологии.
    _earlier = sorted(
        [r for r in history if r.period_id != active_period.id and _rkey(r) < _target_key],
        key=_rkey, reverse=True,
    )
    prev_latest = next((r for r in _earlier if is_meaningful_prev(r)), None)
    prev_any = _earlier[0] if _earlier else None  # для prev_is_synth-detection

    p_hot, p_cold, p_elect = prev_latest.hot_water if prev_latest else ZERO, prev_latest.cold_water if prev_latest else ZERO, prev_latest.electricity if prev_latest else ZERO

    # Раздельная подача (только для save_manual_entry; в create_one_time_charge
    # _provided всегда True — поведение прежнее): для НЕпереданных ресурсов
    # используем prev → счётчик «не двигается», дельта 0, расход не начисляется.
    hot_to_save = data.hot_water if hot_provided else p_hot
    cold_to_save = data.cold_water if cold_provided else p_cold
    elect_to_save = data.electricity if elect_provided else p_elect

    # synth-baseline detection: meaningful prev отсутствует, но какой-то
    # AUTO_GENERATED/DATA_OVERFLOW_RESET в истории есть. Тогда delta надо
    # проверять строже (см. validate_meter_reading.prev_is_synth). Кейс
    # Пегарькова — без этой проверки он подаёт 161/340 поверх AUTO_GENERATED
    # 0/0/0 и получает счёт 81 485 ₽.
    _prev_is_synth = (prev_latest is None) and (prev_any is not None)
    # prev_for_validator: при synth — это значения synth-записи (обычно 0),
    # при нормальном prev — реальные предыдущие. При полном отсутствии — None.
    if _prev_is_synth:
        _val_prev_hot, _val_prev_cold, _val_prev_elect = (
            prev_any.hot_water, prev_any.cold_water, prev_any.electricity,
        )
    elif prev_latest is not None:
        _val_prev_hot, _val_prev_cold, _val_prev_elect = p_hot, p_cold, p_elect
    else:
        _val_prev_hot = _val_prev_cold = _val_prev_elect = None

    # Ручной ввод админом НЕ блокируем монотонностью/дельтой/потолком
    # (fix 2026-06-16): админ авторитетен — вписывает показания за ЛЮБОЙ месяц
    # в ЛЮБУЮ сторону (доввод за апрель/март поверх мая, правка «его же» цифр)
    # без ложных ошибок «счётчик не может уменьшаться». Единственный
    # предохранитель от катастрофы (пропущенная точка → счёт в сотни тысяч) —
    # финальная validate_total_cost ниже. Флаги аномалий считаются
    # check_reading_for_anomalies_v2 и видны в реестре, но НЕ блокируют.
    _ = (_val_prev_hot, _val_prev_cold, _val_prev_elect, _prev_is_synth)

    d_hot = (hot_to_save - p_hot) if hot_provided else ZERO
    d_cold = (cold_to_save - p_cold) if cold_provided else ZERO
    d_elect = (elect_to_save - p_elect) if elect_provided else ZERO

    residents_count = paying_residents(user, room)
    total_room = room.total_room_residents if room.total_room_residents > 0 else 1

    user_share_elect = (Decimal(residents_count) / Decimal(total_room)) * d_elect

    # BASELINE: если по комнате нет утверждённой истории — первая подача,
    # все cost_* = 0 (счётчики могут быть «накрученные» за годы, см. также
    # approve_single / bulk_approve_drafts / client save_reading). Флаг
    # BASELINE попадёт в реестр, чтобы админ не искал «откуда ноль».
    is_baseline = prev_latest is None
    # Сезонные флаги: global emergency override AND per-tariff (heating_active + даты).
    # См. комментарий в client_readings POST /api/calculate.
    from app.modules.utility.routers.settings import _load_seasonal
    _seasonal = await _load_seasonal(db)
    _heating = _seasonal.heating_season_active and t.is_heating_active_now()
    _hw = _seasonal.hot_water_heating_active and t.is_hw_heating_active_now()
    if is_baseline:
        # Bug L: area-based начисления (содержание/найм/ТКО/отопление)
        # платятся ВСЕГДА, даже при первой подаче. Вызываем calculate_utilities
        # с volume_*=0 → water/sewage = 0, area-based = area × tariff.
        costs = calculate_utilities(
            user=user, room=room, tariff=t,
            volume_hot=ZERO, volume_cold=ZERO,
            volume_sewage=ZERO, volume_electricity_share=ZERO,
            heating_season_active=_heating,
            hot_water_heating_active=_hw,
        )
    else:
        costs = calculate_utilities(
            user=user, room=room, tariff=t, volume_hot=d_hot, volume_cold=d_cold,
            volume_sewage=d_hot + d_cold, volume_electricity_share=user_share_elect,
            heating_season_active=_heating,
            hot_water_heating_active=_hw,
        )

        # Финальная sanity-проверка: total_cost не должен превышать MAX_TOTAL_COST_PER_READING
        # (обычно 100k ₽/период). Защита от того что расчёт всё-таки прошёл валидацию
        # дельт, но итог получился нереалистичный (большая площадь × большая дельта).
        from app.modules.utility.services.reading_validators import validate_total_cost
        _tc = validate_total_cost(costs["total_cost"])
        if not _tc.ok:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Итог расчёта нереалистичен: "
                    + "; ".join(_tc.errors)
                    + ". Проверьте показания и тариф."
                ),
            )

    temp_reading = MeterReading(hot_water=hot_to_save, cold_water=cold_to_save, electricity=elect_to_save)
    flags, score = check_reading_for_anomalies_v2(temp_reading, history, user=user, room=room)
    if is_baseline:
        flags, score = "BASELINE", 0

    adj_map = {row[0]: (row[1] or ZERO) for row in
               (await db.execute(select(Adjustment.account_type, func.sum(Adjustment.amount))
               .where(Adjustment.user_id == user.id, Adjustment.period_id == active_period.id).group_by(Adjustment.account_type))).all()}

    # Bug AL: ищем существующее показание ЭТОГО ЖИЛЬЦА в активном периоде —
    # сначала draft, потом approved. Если admin вводит «поверх» подачи через
    # Excel/gsheets, мы должны обновить approved, а не создавать второй
    # reading для того же user_id+period.
    draft = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved.is_(False),
            MeterReading.period_id == active_period.id,
        )
    )).scalars().first()

    approved_current = None
    if not draft:
        approved_current = (await db.execute(
            select(MeterReading).where(
                MeterReading.user_id == user.id,
                MeterReading.period_id == active_period.id,
                MeterReading.is_approved.is_(True),
            ).order_by(MeterReading.created_at.desc()).limit(1)
        )).scalars().first()

    target = draft or approved_current

    # Долг/переплата 1С НЕ в ИТОГО (30.05.2026) — только начисление + корректировки.
    # У target поля debt_*/overpayment_* НЕ перезаписываются (см. ниже) —
    # накопленное сальдо 1С сохраняется как есть.
    total_209 = (costs['total_cost'] - costs['cost_social_rent']) + adj_map.get('209', ZERO)
    total_205 = costs['cost_social_rent'] + adj_map.get('205', ZERO)

    # За ЗАКРЫТЫЙ (прошлый) период ручной ввод сразу УТВЕРЖДАЕМ (fix 2026-06-17):
    # черновик в закрытом периоде никогда не утвердится (закрытие уже было) —
    # «висел» бы неприменённым. Админ вводит задним числом → применяем сразу.
    is_past_closed = not bool(active_period.is_active)

    if target:
        # Обновляем существующий reading (draft или approved).
        # Bug AL: при перезаписи approved оставляем is_approved=True —
        # админ намеренно корректирует утверждённую квитанцию.
        target.hot_water, target.cold_water, target.electricity = hot_to_save, cold_to_save, elect_to_save
        target.anomaly_flags, target.anomaly_score = flags, score
        for k, v in costs_for_model_fields(costs).items():
            setattr(target, k, v)
        target.total_209, target.total_205, target.total_cost = total_209, total_205, total_209 + total_205
        if is_past_closed and not target.is_approved:
            target.is_approved = True
        src_reading = target
    else:
        src_reading = MeterReading(
            user_id=user.id, room_id=room.id, period_id=active_period.id,
            hot_water=hot_to_save, cold_water=cold_to_save, electricity=elect_to_save,
            debt_209=ZERO, overpayment_209=ZERO, debt_205=ZERO, overpayment_205=ZERO,
            total_209=total_209, total_205=total_205, total_cost=total_209 + total_205,
            is_approved=is_past_closed, anomaly_flags=flags, anomaly_score=score,
            **costs_for_model_fields(costs)
        )
        db.add(src_reading)

    # Доввод за прошлый месяц → пересчитываем цепочку реальных показаний ЭТОГО
    # жильца (источника): следующий месяц (май) подхватит свежее prev (апрель) и
    # его суммы пересчитаются «сразу». Для активного периода не трогаем.
    # ВАЖНО (ревью 2026-06-17): пересчёт делаем ДО тиражирования и ТОЛЬКО для
    # источника — клоны холостяков НЕ пересчитываем по цепочке (их значения =
    # авторитетная делёная доля источника; повторный пересчёт по их собственному
    # prev исказил бы дельту).
    recalced = 0
    if is_past_closed:
        await db.flush()  # чтобы только что созданный reading попал в цепочку
        try:
            recalced = await _recompute_real_chain(db, user, room, t)
        except Exception as ex:
            logging.getLogger(__name__).warning("[manual] recompute chain failed: %s", ex)

    # Холостяцкая квартира: коммуналка делится ПОРОВНУ. Берём ИТОГОВЫЕ (после
    # возможного пересчёта) суммы источника — уже доля одного человека
    # (calculate_utilities поделил на total_room_residents) — и копируем на всех
    # остальных активных жильцов квартиры (иначе им шёл бы только baseline).
    # Делалось только в подаче жильцом — для ручного ввода добавлено 2026-06-17.
    singles_affected = []
    if bool(getattr(room, "is_singles_apartment", False)):
        await db.flush()
        from app.modules.utility.services.singles_billing import propagate_singles_reading
        _final_costs = {f: getattr(src_reading, f) for f in MODEL_COST_FIELDS}
        singles_affected = await propagate_singles_reading(
            db, room=room, period_id=active_period.id, source_user_id=user.id,
            hot=src_reading.hot_water, cold=src_reading.cold_water,
            elect=src_reading.electricity, costs=_final_costs,
            total_209=src_reading.total_209, total_205=src_reading.total_205,
            flags=src_reading.anomaly_flags, is_approved=bool(src_reading.is_approved),
        )

    await db.flush()  # гарантируем reading_id для нового показания (нужно UI для «Утвердить»)
    reading_id = src_reading.id
    await db.commit()
    return {
        "status": "success",
        "reading_id": reading_id,
        "updated_kind": "draft" if draft else ("approved" if approved_current else "new_draft"),
        "auto_approved": is_past_closed,
        "chain_recalced": recalced,
        "singles_shared": len(singles_affected),
    }


async def create_one_time_charge(db: AsyncSession, data: OneTimeChargeSchema):
    """Разовое (пропорциональное) начисление при выселении или переезде.

    NB: OneTimeChargeSchema требует все 3 значения (раздельная подача только
    в save_manual_entry). Заглушки _provided=True сохраняют поведение в общем
    блоке валидации, который шарится между save_manual_entry и этой функцией.
    """
    # Совместимость с общим блоком валидации (см. save_manual_entry):
    # в charge все три значения всегда заданы по схеме — never partial.
    hot_provided = True
    cold_provided = True
    elect_provided = True

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
            # Ревизия #3 (решение «baseline=0»): period_id=NULL baseline
            # (INITIAL_SETUP) НЕ берём как prev — первая реальная подача = baseline
            # (расход 0), единообразно с approve_single/client/tasks/gsheets.
            MeterReading.period_id.isnot(None),
        )
        .order_by(MeterReading.created_at.desc()).limit(6)
    )).scalars().all()

    # is_meaningful_prev: пропускаем AUTO_GENERATED / DATA_OVERFLOW_RESET /
    # MANUAL_RECEIPT / AUTO_NO_HISTORY — их значения = 0, использовать как
    # baseline для дельты → фантастические суммы при следующей реальной подаче
    # (инцидент may 2026: жилец Капранов получил счёт ~825 000 ₽ потому что
    # prev был AUTO_GENERATED с 0 ГВС → delta = 1 468 м³ × 311 ₽/м³).
    from app.modules.utility.services.reading_calculator import is_meaningful_prev
    prev_latest = next((r for r in history if is_meaningful_prev(r)), None)
    prev_any = history[0] if history else None

    p_hot, p_cold, p_elect = prev_latest.hot_water if prev_latest else ZERO, prev_latest.cold_water if prev_latest else ZERO, prev_latest.electricity if prev_latest else ZERO

    # Раздельная подача (только для save_manual_entry; в create_one_time_charge
    # _provided всегда True — поведение прежнее): для НЕпереданных ресурсов
    # используем prev → счётчик «не двигается», дельта 0, расход не начисляется.
    hot_to_save = data.hot_water if hot_provided else p_hot
    cold_to_save = data.cold_water if cold_provided else p_cold
    elect_to_save = data.electricity if elect_provided else p_elect

    # synth-baseline detection — см. save_manual_entry выше.
    _prev_is_synth = (prev_latest is None) and (prev_any is not None)
    if _prev_is_synth:
        _val_prev_hot, _val_prev_cold, _val_prev_elect = (
            prev_any.hot_water, prev_any.cold_water, prev_any.electricity,
        )
    elif prev_latest is not None:
        _val_prev_hot, _val_prev_cold, _val_prev_elect = p_hot, p_cold, p_elect
    else:
        _val_prev_hot = _val_prev_cold = _val_prev_elect = None

    # Ручной ввод админом НЕ блокируем монотонностью/дельтой/потолком
    # (fix 2026-06-16): админ авторитетен — вписывает показания за ЛЮБОЙ месяц
    # в ЛЮБУЮ сторону (доввод за апрель/март поверх мая, правка «его же» цифр)
    # без ложных ошибок «счётчик не может уменьшаться». Единственный
    # предохранитель от катастрофы (пропущенная точка → счёт в сотни тысяч) —
    # финальная validate_total_cost ниже. Флаги аномалий считаются
    # check_reading_for_anomalies_v2 и видны в реестре, но НЕ блокируют.
    _ = (_val_prev_hot, _val_prev_cold, _val_prev_elect, _prev_is_synth)

    d_hot = (hot_to_save - p_hot) if hot_provided else ZERO
    d_cold = (cold_to_save - p_cold) if cold_provided else ZERO
    d_elect = (elect_to_save - p_elect) if elect_provided else ZERO

    residents_count = paying_residents(user, room)
    total_room = room.total_room_residents if room.total_room_residents > 0 else 1

    user_share_elect = (Decimal(residents_count) / Decimal(total_room)) * d_elect

    # BASELINE: первая в жизни подача по комнате → потребление = 0, но
    # area-based начисления платятся всегда (см. Bug L в save_manual_entry).
    is_baseline = prev_latest is None
    # См. комментарий в save_manual_entry — те же сезонные флаги (global + per-tariff).
    from app.modules.utility.routers.settings import _load_seasonal
    _seasonal = await _load_seasonal(db)
    _heating = _seasonal.heating_season_active and t.is_heating_active_now()
    _hw = _seasonal.hot_water_heating_active and t.is_hw_heating_active_now()
    if is_baseline:
        costs = calculate_utilities(
            user=user, room=room, tariff=t,
            volume_hot=ZERO, volume_cold=ZERO,
            volume_sewage=ZERO, volume_electricity_share=ZERO, fraction=fraction,
            heating_season_active=_heating,
            hot_water_heating_active=_hw,
        )
    else:
        costs = calculate_utilities(
            user=user, room=room, tariff=t, volume_hot=d_hot, volume_cold=d_cold,
            volume_sewage=d_hot + d_cold, volume_electricity_share=user_share_elect, fraction=fraction,
            heating_season_active=_heating,
            hot_water_heating_active=_hw,
        )

    adj_map = {row[0]: (row[1] or ZERO) for row in
               (await db.execute(select(Adjustment.account_type, func.sum(Adjustment.amount))
               .where(Adjustment.user_id == user.id, Adjustment.period_id == active_period.id).group_by(Adjustment.account_type))).all()}

    draft = (await db.execute(
        select(MeterReading).where(MeterReading.room_id == room.id, MeterReading.is_approved.is_(False), MeterReading.period_id == active_period.id)
    )).scalars().first()

    # Долг/переплата 1С НЕ в ИТОГО (30.05.2026) — только начисление + корректировки.
    total_209 = (costs['total_cost'] - costs['cost_social_rent']) + adj_map.get('209', ZERO)
    total_205 = costs['cost_social_rent'] + adj_map.get('205', ZERO)

    charge_flag = "ONE_TIME_CHARGE_BASELINE" if is_baseline else "ONE_TIME_CHARGE"
    if draft:
        draft.hot_water, draft.cold_water, draft.electricity = hot_to_save, cold_to_save, elect_to_save
        draft.anomaly_flags, draft.anomaly_score = charge_flag, 0
        for k, v in costs_for_model_fields(costs).items():
            setattr(draft, k, v)
        draft.total_209, draft.total_205, draft.total_cost, draft.is_approved = total_209, total_205, total_209 + total_205, True
    else:
        db.add(MeterReading(
            user_id=user.id, room_id=room.id, period_id=active_period.id,
            hot_water=hot_to_save, cold_water=cold_to_save, electricity=elect_to_save,
            debt_209=ZERO, overpayment_209=ZERO, debt_205=ZERO, overpayment_205=ZERO,
            total_209=total_209, total_205=total_205, total_cost=total_209 + total_205,
            is_approved=True, anomaly_flags=charge_flag, anomaly_score=0,
            **costs_for_model_fields(costs)
        ))

    room.last_hot_water, room.last_cold_water, room.last_electricity = hot_to_save, cold_to_save, elect_to_save
    db.add(room)

    if data.is_moving_out:
        user.is_deleted = True
        user.username = f"{user.username}_deleted_{user.id}"
        user.login = f"{user.login}_deleted_{user.id}"  # освобождаем и логин
        user.room_id = None

    await db.commit()
    return {"status": "success"}


async def create_manual_receipt(
    db: AsyncSession, user_id: int, period_id: int | None = None,
):
    """Создаёт квитанцию вручную БЕЗ ввода показаний счётчиков.

    Use case: жилец имеет долг или переплату от импорта 1С, но не подал
    показания за текущий период. Админ хочет всё равно сформировать ему
    квитанцию — с нулевым потреблением, но с учётом долгов/переплат и
    фиксированных начислений из тарифа (cost_maintenance, fixed_part).

    Математика:
      cost_* = calculate_utilities(volume=0, ...)  // только фикс-часть
      total_209 = cost_total - cost_social_rent + adj_209   // долг 1С — НЕ здесь
      total_205 = cost_social_rent              + adj_205
      total_cost = total_209 + total_205   // долг/переплата хранятся отдельно

    Источник debt/overpay (приоритет):
      1) draft того же периода (если есть — там может быть свежий импорт 1С)
      2) последний approved reading жильца (debt/overpay переносятся между
         периодами автоматически — это «текущее сальдо»)
      3) 0/0 если истории нет
    """
    target_period = None
    if period_id is not None:
        target_period = await db.get(BillingPeriod, period_id)
    if target_period is None:
        target_period = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active)
        )).scalars().first()
    if not target_period:
        raise HTTPException(400, "Нет активного периода")

    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == user_id)
    )).scalars().first()
    if not user or user.is_deleted:
        raise HTTPException(404, "Жилец не найден")
    room = user.room
    if not room:
        raise HTTPException(400, "Жилец не привязан к помещению")

    # NB: тариф больше не нужен — costs всегда нулевые, фикс-часть не
    # начисляется без подачи показаний. Раньше передавали в calculate_utilities.

    # Последний approved reading жильца в этой комнате — для показаний.
    # История по ПАРЕ (user_id, room_id), чтобы при переезде старая комната
    # не «утянула» данные нового жильца.
    prev = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved.is_(True),
        ).order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    # Защита от дублирования: ищем ЛЮБОЙ reading этого жильца в этом
    # периоде (approved или draft). Раньше искали только drafts → если
    # уже был approved, создавался второй approved — в финансовой
    # отчётности появлялась пара одинаковых жильцов.
    existing = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.period_id == target_period.id,
        ).order_by(MeterReading.created_at.desc())
    )).scalars().all()

    approved_existing = next((r for r in existing if r.is_approved), None)
    if approved_existing:
        raise HTTPException(
            400,
            f"Квитанция за этот период уже есть (reading id={approved_existing.id}). "
            "Чтобы создать новую — удалите старую через реестр показаний."
        )

    # Берём draft (если есть) — будем апдейтить его до approved
    draft = next((r for r in existing if not r.is_approved), None)

    # Долги/переплаты по 209 и 205 счетам берём НЕЗАВИСИМО из самых
    # свежих reading-ов где есть ненулевое сальдо. Раньше брали один
    # reading на все 4 поля → если 209-импорт в Мае, а 205-импорт в
    # Январе → 205-сальдо терялось (брался свежий 209-reading где 205=0).

    # Свежий reading с 209-балансом
    latest_209 = (await db.execute(
        select(MeterReading).where(
            MeterReading.room_id == room.id,
            (MeterReading.debt_209 > 0) | (MeterReading.overpayment_209 > 0),
        ).order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    # Свежий reading с 205-балансом
    latest_205 = (await db.execute(
        select(MeterReading).where(
            MeterReading.room_id == room.id,
            (MeterReading.debt_205 > 0) | (MeterReading.overpayment_205 > 0),
        ).order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    # Priority: draft текущего периода (свежий импорт) → независимо 209/205.
    if draft and ((draft.debt_209 or 0) > 0 or (draft.overpayment_209 or 0) > 0):
        debt_209 = draft.debt_209 or ZERO
        overpay_209 = draft.overpayment_209 or ZERO
    else:
        debt_209 = (latest_209.debt_209 if latest_209 else ZERO) or ZERO
        overpay_209 = (latest_209.overpayment_209 if latest_209 else ZERO) or ZERO

    if draft and ((draft.debt_205 or 0) > 0 or (draft.overpayment_205 or 0) > 0):
        debt_205 = draft.debt_205 or ZERO
        overpay_205 = draft.overpayment_205 or ZERO
    else:
        debt_205 = (latest_205.debt_205 if latest_205 else ZERO) or ZERO
        overpay_205 = (latest_205.overpayment_205 if latest_205 else ZERO) or ZERO

    # Adjustments периода
    adj_map = {row[0]: (row[1] or ZERO) for row in (await db.execute(
        select(Adjustment.account_type, func.sum(Adjustment.amount))
        .where(Adjustment.user_id == user.id, Adjustment.period_id == target_period.id)
        .group_by(Adjustment.account_type)
    )).all()}

    # manual_receipt — БЕЗ начислений. Жилец не подал показания за этот
    # период, поэтому фикс-часть тарифа (cost_maintenance, cost_social_rent,
    # cost_fixed_part) НЕ начисляется. Только перенос сальдо.
    #
    # Раньше при manual_receipt начислялись фикс-составляющие (~700 ₽/мес
    # за площадь 33м²: наём + содержание + отопление + ТКО). Эти суммы
    # автоматически списывались с переплаты жильца → жилец «терял» деньги
    # за период когда даже не подавал показания. Семантически неверно:
    # фактическая оплата фикс-части должна начисляться когда жилец
    # подтверждает наличие активного потребления (т.е. подаёт показания).
    costs = {
        "cost_hot_water": ZERO, "cost_cold_water": ZERO, "cost_sewage": ZERO,
        "cost_electricity": ZERO, "cost_maintenance": ZERO, "cost_social_rent": ZERO,
        "cost_waste": ZERO, "cost_fixed_part": ZERO, "total_cost": ZERO,
    }

    # Долг/переплата 1С НЕ в ИТОГО (30.05.2026): manual_receipt без показаний =
    # нулевое начисление. Долг/переплата хранятся в reading.debt_*/overpayment_*
    # (записываются ниже) и показываются отдельной справкой в квитанции. ИТОГО =
    # только ручные корректировки (обычно 0).
    total_209 = adj_map.get("209", ZERO)
    total_205 = adj_map.get("205", ZERO)

    # Показания счётчиков = prev (нулевое потребление в текущем периоде)
    hot = prev.hot_water if prev else None
    cold = prev.cold_water if prev else None
    elect = prev.electricity if prev else None

    if draft:
        # Обновляем существующий черновик до approved
        draft.hot_water = hot
        draft.cold_water = cold
        draft.electricity = elect
        draft.debt_209 = debt_209
        draft.overpayment_209 = overpay_209
        draft.debt_205 = debt_205
        draft.overpayment_205 = overpay_205
        draft.anomaly_flags = "MANUAL_RECEIPT"
        draft.anomaly_score = 0
        for k, v in costs_for_model_fields(costs).items():
            setattr(draft, k, v)
        draft.total_209 = total_209
        draft.total_205 = total_205
        # total_cost синхронизируется триггером trg_readings_sync_total_cost
        # из total_209+total_205, но для надёжности выставим явно
        draft.total_cost = total_209 + total_205
        draft.is_approved = True
        result_reading = draft
    else:
        new = MeterReading(
            user_id=user.id, room_id=room.id, period_id=target_period.id,
            hot_water=hot, cold_water=cold, electricity=elect,
            debt_209=debt_209, overpayment_209=overpay_209,
            debt_205=debt_205, overpayment_205=overpay_205,
            total_209=total_209, total_205=total_205,
            total_cost=total_209 + total_205,
            is_approved=True,
            anomaly_flags="MANUAL_RECEIPT",
            anomaly_score=0,
            **costs_for_model_fields(costs),
        )
        db.add(new)
        await db.flush()
        result_reading = new

    await db.commit()
    return {
        "status": "success",
        "reading_id": result_reading.id,
        "total_209": float(total_209),
        "total_205": float(total_205),
        "total_cost": float(total_209 + total_205),
        "is_overpayment": (total_209 + total_205) < 0,
    }


async def bulk_create_manual_receipts(
    db: AsyncSession, period_id: int | None = None,
) -> dict:
    """Массовое создание квитанций для жильцов которые НЕ подали показания.

    Use case: в конце периода многие жильцы не подают показания. Админ
    хочет за всех создать квитанции одной кнопкой — только сальдо, без
    начислений (см. create_manual_receipt).

    Алгоритм:
      1. Найти всех User с room (не deleted, role=user) активного жилфонда
      2. Отфильтровать тех у кого УЖЕ есть approved MeterReading в
         целевом периоде — для них пропуск (квитанция уже есть)
      3. Для остальных вызвать create_manual_receipt поштучно — там
         корректно собрано debt/overpay из любых периодов
      4. Не падать на ошибке отдельного жильца — логировать и продолжать

    Returns:
      {processed, created, skipped_existing, errors}
    """
    target_period = None
    if period_id is not None:
        target_period = await db.get(BillingPeriod, period_id)
    if target_period is None:
        target_period = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active)
        )).scalars().first()
    if not target_period:
        raise HTTPException(400, "Нет активного периода")

    # 1. Все активные жильцы с комнатой
    all_users = (await db.execute(
        select(User).options(selectinload(User.room)).where(
            User.is_deleted.is_(False),
            User.role == "user",
            User.room_id.is_not(None),
        )
    )).scalars().all()

    # 2. У кого уже есть approved reading в целевом периоде — пропустить
    existing_approved_user_ids = set((await db.execute(
        select(MeterReading.user_id).where(
            MeterReading.period_id == target_period.id,
            MeterReading.is_approved.is_(True),
            MeterReading.user_id.is_not(None),
        )
    )).scalars().all())

    created = 0
    skipped_existing = 0
    errors: list[dict] = []

    for user in all_users:
        if user.id in existing_approved_user_ids:
            skipped_existing += 1
            continue
        try:
            await create_manual_receipt(db, user.id, target_period.id)
            created += 1
        except HTTPException as e:
            # 400 «уже есть» / «нет комнаты» — пропускаем, не критично
            if e.status_code == 400:
                skipped_existing += 1
            else:
                errors.append({"user_id": user.id, "username": user.username, "error": e.detail})
        except Exception as e:
            errors.append({"user_id": user.id, "username": user.username, "error": str(e)[:200]})

    return {
        "status": "ok",
        "period_id": target_period.id,
        "period_name": target_period.name,
        "total_users": len(all_users),
        "created": created,
        "skipped_existing": skipped_existing,
        "errors": errors[:50],  # ограничиваем длину response
        "errors_total": len(errors),
    }


async def delete_reading(
    db: AsyncSession,
    reading_id: int,
    actor: Optional["User"] = None,
):
    """Удаление утверждённого/чернового MeterReading.

    ИСПРАВЛЕНИЕ 500-ОШИБКИ (apr 2026):
      1. Раньше использовался `db.get(MeterReading, reading_id)` — но PK
         у MeterReading составной (id + created_at, models.py:289-290),
         и db.get для составного PK ожидает tuple, а не scalar. В итоге
         либо None (404), либо TypeError (500). Заменили на explicit
         SELECT WHERE id=:id (id всё равно уникален из-за SERIAL).

      2. На уровне БД FK от gsheets_import_rows.reading_id к readings.id
         ФИЗИЧЕСКИ НЕ СОЗДАН — readings партиционированная и PostgreSQL
         не разрешает FK на партиционированные таблицы (см. комментарий
         в миграции gsheets_001_import_rows). Поэтому DROP не падает на
         FK violation — но логически gsheets-строки могут остаться
         «висеть» с reading_id, указывающим на удалённый reading.
         Чтобы такого orphan'а не было, явно обнуляем reading_id:
         status='auto_approved' сохраняем — следующий
         promote_auto_approved_rows() подхватит строки и создаст
         для них новый MeterReading автоматически.

    AUDIT LOG (may 2026): добавлена запись в audit_log при удалении —
    раньше при разборе stuck-drafts админ удалял reading и след пропадал.
    Юридически это важно: квитанции — это деньги, изменения нужно
    отслеживать. Сохраняем username/full_name/period_id/значения чтобы
    можно было восстановить картину «что было до удаления».
    """
    from app.modules.utility.models import GSheetsImportRow, User
    from sqlalchemy import update
    from sqlalchemy.orm import selectinload
    from app.modules.utility.routers.admin_dashboard import write_audit_log

    res = await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.user).selectinload(User.room))
        .where(MeterReading.id == reading_id)
    )
    reading = res.scalars().first()
    if not reading:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    # Снапшот для audit_log (после delete доступ к полям недостоверен).
    target_user = reading.user
    room = target_user.room if target_user else None
    snapshot = {
        "reading_id": reading_id,
        "period_id": reading.period_id,
        "is_approved": bool(reading.is_approved),
        "hot_water": str(reading.hot_water or 0),
        "cold_water": str(reading.cold_water or 0),
        "electricity": str(reading.electricity or 0),
        "total_cost": str(reading.total_cost or 0),
        "anomaly_flags": reading.anomaly_flags,
        "target_user_id": target_user.id if target_user else None,
        "target_username": target_user.username if target_user else None,
        "target_full_name": target_user.full_name if target_user else None,
        "dormitory": room.dormitory_name if room else None,
        "room_number": room.room_number if room else None,
    }

    # Отвязываем gsheets-строки, которые ссылались на это reading.
    # Без этого orphan-ссылки запутают админский UI и promote-задачу.
    await db.execute(
        update(GSheetsImportRow)
        .where(GSheetsImportRow.reading_id == reading_id)
        .values(reading_id=None, processed_at=None)
    )

    await db.delete(reading)

    # Audit. Если actor не передан (legacy caller) — лог пропускаем, но
    # удаление всё равно проходит — backward-compat.
    if actor is not None:
        try:
            await write_audit_log(
                db, actor.id, actor.username,
                action="delete_reading",
                entity_type="meter_reading",
                entity_id=reading_id,
                details=snapshot,
            )
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.warning("audit_log for delete_reading failed: %s", exc)

    await db.commit()
    return {"status": "deleted"}


async def convert_reading_to_baseline(
    db: AsyncSession,
    reading_id: int,
    actor: Optional["User"] = None,
) -> dict:
    """Превратить аномальный reading в Начальный период (baseline).

    Use case: жилец впервые подал реальные показания счётчика (например,
    ГВС=2186, ХВС=4112 у Струковой — счётчик уже накручен за годы), но в
    БД его «Начальный период» = AUTO_GENERATED 0/0/0. В результате дельта
    от 0 до 2186 → счёт 12 653 ₽ на ровном месте. После этой операции:
      - Значения reading'а перенесены в INITIAL_SETUP-запись (single
        источник истины для baseline данной комнаты);
      - Текущий аномальный reading удалён (вместе с его total_cost);
      - Room.last_* обновлены — следующая подача от жильца будет иметь
        корректную дельту относительно реального baseline.

    Audit log: оба действия (создание/обновление initial + удаление reading)
    логируются. Юридически важно — это деньги на квитанции.
    """
    from app.modules.utility.models import User, GSheetsImportRow
    from sqlalchemy import update
    from sqlalchemy.orm import selectinload
    from app.modules.utility.routers.admin_dashboard import write_audit_log

    res = await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.user).selectinload(User.room))
        .where(MeterReading.id == reading_id)
    )
    reading = res.scalars().first()
    if not reading:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    target_user = reading.user
    room = target_user.room if target_user else None
    if not room:
        raise HTTPException(
            status_code=400,
            detail="У жильца reading'а нет привязанной комнаты — нельзя "
                   "превратить в baseline без room_id.",
        )

    new_hot = reading.hot_water or Decimal("0")
    new_cold = reading.cold_water or Decimal("0")
    new_elect = reading.electricity or Decimal("0")

    # Снапшот удаляемого reading'а для audit.
    snapshot = {
        "reading_id": reading_id,
        "period_id": reading.period_id,
        "is_approved": bool(reading.is_approved),
        "hot_water": str(new_hot),
        "cold_water": str(new_cold),
        "electricity": str(new_elect),
        "total_cost": str(reading.total_cost or 0),
        "anomaly_flags": reading.anomaly_flags,
        "target_user_id": target_user.id if target_user else None,
        "target_username": target_user.username if target_user else None,
        "target_full_name": target_user.full_name if target_user else None,
        "dormitory": room.dormitory_name,
        "room_number": room.room_number,
    }

    # Ищем существующий baseline-reading. Сначала INITIAL_SETUP (приоритет),
    # потом AUTO_GENERATED (то что система генерит при онбординге).
    initial_q = await db.execute(
        select(MeterReading).where(
            MeterReading.room_id == room.id,
            MeterReading.anomaly_flags.in_([
                "INITIAL_SETUP",
                "INITIAL_FROM_FIRST_SUBMISSION",
                "AUTO_GENERATED",
            ]),
        ).order_by(MeterReading.created_at.desc())
    )
    initial = initial_q.scalars().first()

    if initial is not None:
        # Обновляем существующий baseline.
        initial.hot_water = new_hot
        initial.cold_water = new_cold
        initial.electricity = new_elect
        initial.anomaly_flags = "INITIAL_FROM_FIRST_SUBMISSION"
        initial.anomaly_score = 0
        initial.is_approved = True
        db.add(initial)
        initial_id = initial.id
        initial_action = "updated"
    else:
        # Создаём новый INITIAL_SETUP. period_id=NULL — baseline не привязан
        # к конкретному периоду (см. set_initial_readings выше).
        initial = MeterReading(
            room_id=room.id,
            user_id=target_user.id if target_user else None,
            period_id=None,
            hot_water=new_hot,
            cold_water=new_cold,
            electricity=new_elect,
            is_approved=True,
            anomaly_flags="INITIAL_FROM_FIRST_SUBMISSION",
            anomaly_score=0,
            total_209=Decimal("0"),
            total_205=Decimal("0"),
        )
        db.add(initial)
        await db.flush()
        initial_id = initial.id
        initial_action = "created"

    # Обновляем кэш Room.last_* — это критично, потому что reading_calculator
    # местами берёт значения именно из Room (быстрая ветка без SELECT по
    # MeterReading). Без обновления первая же новая подача даст delta от 0.
    room.last_hot_water = new_hot
    room.last_cold_water = new_cold
    room.last_electricity = new_elect
    db.add(room)

    # Отвязываем gsheets-строки от удаляемого reading'а — иначе orphan-ссылки
    # запутают promote-задачу (см. delete_reading выше).
    await db.execute(
        update(GSheetsImportRow)
        .where(GSheetsImportRow.reading_id == reading_id)
        .values(reading_id=None, processed_at=None)
    )

    await db.delete(reading)

    if actor is not None:
        try:
            await write_audit_log(
                db, actor.id, actor.username,
                action="convert_reading_to_baseline",
                entity_type="meter_reading",
                entity_id=reading_id,
                details={
                    **snapshot,
                    "baseline_action": initial_action,
                    "baseline_reading_id": initial_id,
                },
            )
        except Exception as exc:
            logger = logging.getLogger(__name__)
            logger.warning("audit_log for convert_reading_to_baseline failed: %s", exc)

    await db.commit()
    return {
        "status": "ok",
        "baseline_action": initial_action,
        "baseline_reading_id": initial_id,
        "removed_reading_id": reading_id,
        "values": {
            "hot_water": str(new_hot),
            "cold_water": str(new_cold),
            "electricity": str(new_elect),
        },
    }
