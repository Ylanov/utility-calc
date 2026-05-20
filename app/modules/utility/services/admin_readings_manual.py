# app/modules/utility/services/admin_readings_manual.py
from decimal import Decimal
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
)
from app.modules.utility.services.anomaly_detector import check_reading_for_anomalies_v2

ZERO = Decimal("0.00")

async def save_manual_entry(db: AsyncSession, data: AdminManualReadingSchema):
    """Сохранение черновика бухгалтером вручную.

    Если data.period_id задан — используем его (для ввода за прошлый
    месяц). Если None — берём текущий active_period (back-compat).
    """
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

    # Через единый кеш — Room.tariff_id побеждает User.tariff_id (см. tariff_cache.py).
    from app.modules.utility.services.tariff_cache import tariff_cache
    t = tariff_cache.get_effective_tariff(user=user, room=room) or \
        (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()

    # История считается ПО ЖИЛЬЦУ В ЭТОЙ КОМНАТЕ, не по комнате в целом.
    # Если до этого жильца тут были показания (старый жилец, GSHEETS_AUTO
    # и т.п.), их учитывать нельзя — получились бы миллионы.
    history = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved,
        )
        .order_by(MeterReading.created_at.desc()).limit(6)
    )).scalars().all()

    prev_latest = history[0] if history else None
    prev_manual = next((r for r in history if r.anomaly_flags != "AUTO_GENERATED"), None)

    p_hot_man, p_cold_man, p_elect_man = prev_manual.hot_water if prev_manual else ZERO, prev_manual.cold_water if prev_manual else ZERO, prev_manual.electricity if prev_manual else ZERO

    # Единая sanity-валидация (см. reading_validators.py): абсолютные пороги,
    # неотрицательность, монотонность, разумные дельты. Защита от случая
    # когда админ вводил гигантские значения (тестирование, пропущенная точка).
    from app.modules.utility.services.reading_validators import validate_meter_reading
    _vresult = validate_meter_reading(
        hot=data.hot_water, cold=data.cold_water, elect=data.electricity,
        prev_hot=p_hot_man, prev_cold=p_cold_man, prev_elect=p_elect_man,
        is_baseline=(prev_latest is None),
    )
    if not _vresult.ok:
        raise HTTPException(400, "; ".join(_vresult.errors))

    p_hot, p_cold, p_elect = prev_latest.hot_water if prev_latest else ZERO, prev_latest.cold_water if prev_latest else ZERO, prev_latest.electricity if prev_latest else ZERO
    d_hot, d_cold, d_elect = data.hot_water - p_hot, data.cold_water - p_cold, data.electricity - p_elect

    residents_count = user.residents_count if user.residents_count is not None else 1
    total_room = room.total_room_residents if room.total_room_residents > 0 else 1

    user_share_elect = (Decimal(residents_count) / Decimal(total_room)) * d_elect

    # BASELINE: если по комнате нет утверждённой истории — первая подача,
    # все cost_* = 0 (счётчики могут быть «накрученные» за годы, см. также
    # approve_single / bulk_approve_drafts / client save_reading). Флаг
    # BASELINE попадёт в реестр, чтобы админ не искал «откуда ноль».
    is_baseline = prev_latest is None
    if is_baseline:
        costs = {
            "cost_hot_water": ZERO, "cost_cold_water": ZERO,
            "cost_sewage": ZERO, "cost_electricity": ZERO,
            "cost_maintenance": ZERO, "cost_social_rent": ZERO,
            "cost_waste": ZERO, "cost_fixed_part": ZERO,
            "total_cost": ZERO,
        }
    else:
        # Сезонные флаги: global emergency override AND per-tariff (heating_active + даты).
        # См. комментарий в client_readings POST /api/calculate.
        from app.modules.utility.routers.settings import _load_seasonal
        _seasonal = await _load_seasonal(db)
        _heating = _seasonal.heating_season_active and t.is_heating_active_now()
        _hw = _seasonal.hot_water_heating_active and t.is_hw_heating_active_now()
        costs = calculate_utilities(
            user=user, room=room, tariff=t, volume_hot=d_hot, volume_cold=d_cold,
            volume_sewage=d_hot + d_cold, volume_electricity_share=user_share_elect,
            heating_season_active=_heating,
            hot_water_heating_active=_hw,
        )

    temp_reading = MeterReading(hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity)
    flags, score = check_reading_for_anomalies_v2(temp_reading, history, user=user)
    if is_baseline:
        flags, score = "BASELINE", 0

    adj_map = {row[0]: (row[1] or ZERO) for row in
               (await db.execute(select(Adjustment.account_type, func.sum(Adjustment.amount))
               .where(Adjustment.user_id == user.id, Adjustment.period_id == active_period.id).group_by(Adjustment.account_type))).all()}

    draft = (await db.execute(
        select(MeterReading).where(MeterReading.room_id == room.id, MeterReading.is_approved.is_(False), MeterReading.period_id == active_period.id)
    )).scalars().first()

    total_209 = (costs['total_cost'] - costs['cost_social_rent']) + (draft.debt_209 or ZERO if draft else ZERO) - (draft.overpayment_209 or ZERO if draft else ZERO) + adj_map.get('209', ZERO)
    total_205 = costs['cost_social_rent'] + (draft.debt_205 or ZERO if draft else ZERO) - (draft.overpayment_205 or ZERO if draft else ZERO) + adj_map.get('205', ZERO)

    if draft:
        draft.hot_water, draft.cold_water, draft.electricity = data.hot_water, data.cold_water, data.electricity
        draft.anomaly_flags, draft.anomaly_score = flags, score
        for k, v in costs_for_model_fields(costs).items():
            setattr(draft, k, v)
        draft.total_209, draft.total_205, draft.total_cost = total_209, total_205, total_209 + total_205
    else:
        db.add(MeterReading(
            user_id=user.id, room_id=room.id, period_id=active_period.id,
            hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity,
            debt_209=ZERO, overpayment_209=ZERO, debt_205=ZERO, overpayment_205=ZERO,
            total_209=total_209, total_205=total_205, total_cost=total_209 + total_205,
            is_approved=False, anomaly_flags=flags, anomaly_score=score,
            **costs_for_model_fields(costs)
        ))

    await db.commit()
    return {"status": "success"}


async def create_one_time_charge(db: AsyncSession, data: OneTimeChargeSchema):
    """Разовое (пропорциональное) начисление при выселении или переезде."""
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
        )
        .order_by(MeterReading.created_at.desc()).limit(6)
    )).scalars().all()

    prev_latest = history[0] if history else None
    prev_manual = next((r for r in history if r.anomaly_flags != "AUTO_GENERATED"), None)

    p_hot_man, p_cold_man, p_elect_man = prev_manual.hot_water if prev_manual else ZERO, prev_manual.cold_water if prev_manual else ZERO, prev_manual.electricity if prev_manual else ZERO

    # Единая sanity-валидация (см. reading_validators.py): абсолютные пороги,
    # неотрицательность, монотонность, разумные дельты. Защита от случая
    # когда админ вводил гигантские значения (тестирование, пропущенная точка).
    from app.modules.utility.services.reading_validators import validate_meter_reading
    _vresult = validate_meter_reading(
        hot=data.hot_water, cold=data.cold_water, elect=data.electricity,
        prev_hot=p_hot_man, prev_cold=p_cold_man, prev_elect=p_elect_man,
        is_baseline=(prev_latest is None),
    )
    if not _vresult.ok:
        raise HTTPException(400, "; ".join(_vresult.errors))

    p_hot, p_cold, p_elect = prev_latest.hot_water if prev_latest else ZERO, prev_latest.cold_water if prev_latest else ZERO, prev_latest.electricity if prev_latest else ZERO
    d_hot, d_cold, d_elect = data.hot_water - p_hot, data.cold_water - p_cold, data.electricity - p_elect

    residents_count = user.residents_count if user.residents_count is not None else 1
    total_room = room.total_room_residents if room.total_room_residents > 0 else 1

    user_share_elect = (Decimal(residents_count) / Decimal(total_room)) * d_elect

    # BASELINE: первая в жизни подача по комнате → 0 (см. комментарий выше).
    # Для разового начисления это редкий сценарий (обычно у выселяющегося
    # уже есть история), но технически возможен — страхуемся.
    is_baseline = prev_latest is None
    if is_baseline:
        costs = {
            "cost_hot_water": ZERO, "cost_cold_water": ZERO,
            "cost_sewage": ZERO, "cost_electricity": ZERO,
            "cost_maintenance": ZERO, "cost_social_rent": ZERO,
            "cost_waste": ZERO, "cost_fixed_part": ZERO,
            "total_cost": ZERO,
        }
    else:
        # См. комментарий в save_manual_entry — те же сезонные флаги (global + per-tariff).
        from app.modules.utility.routers.settings import _load_seasonal
        _seasonal = await _load_seasonal(db)
        _heating = _seasonal.heating_season_active and t.is_heating_active_now()
        _hw = _seasonal.hot_water_heating_active and t.is_hw_heating_active_now()
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

    total_209 = (costs['total_cost'] - costs['cost_social_rent']) + (draft.debt_209 or ZERO if draft else ZERO) - (draft.overpayment_209 or ZERO if draft else ZERO) + adj_map.get('209', ZERO)
    total_205 = costs['cost_social_rent'] + (draft.debt_205 or ZERO if draft else ZERO) - (draft.overpayment_205 or ZERO if draft else ZERO) + adj_map.get('205', ZERO)

    charge_flag = "ONE_TIME_CHARGE_BASELINE" if is_baseline else "ONE_TIME_CHARGE"
    if draft:
        draft.hot_water, draft.cold_water, draft.electricity = data.hot_water, data.cold_water, data.electricity
        draft.anomaly_flags, draft.anomaly_score = charge_flag, 0
        for k, v in costs_for_model_fields(costs).items():
            setattr(draft, k, v)
        draft.total_209, draft.total_205, draft.total_cost, draft.is_approved = total_209, total_205, total_209 + total_205, True
    else:
        db.add(MeterReading(
            user_id=user.id, room_id=room.id, period_id=active_period.id,
            hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity,
            debt_209=ZERO, overpayment_209=ZERO, debt_205=ZERO, overpayment_205=ZERO,
            total_209=total_209, total_205=total_205, total_cost=total_209 + total_205,
            is_approved=True, anomaly_flags=charge_flag, anomaly_score=0,
            **costs_for_model_fields(costs)
        ))

    room.last_hot_water, room.last_cold_water, room.last_electricity = data.hot_water, data.cold_water, data.electricity
    db.add(room)

    if data.is_moving_out:
        user.is_deleted = True
        user.username = f"{user.username}_deleted_{user.id}"
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
      total_209 = cost_total - cost_social_rent + debt_209 - overpay_209 + adj_209
      total_205 = cost_social_rent              + debt_205 - overpay_205 + adj_205
      total_cost = total_209 + total_205   // МОЖЕТ БЫТЬ < 0 = переплата

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

    # Total = только сальдо (долг минус переплата) + ручные корректировки.
    # Если переплата больше долга — total отрицательный (остаток на счёте).
    total_209 = debt_209 - overpay_209 + adj_map.get("209", ZERO)
    total_205 = debt_205 - overpay_205 + adj_map.get("205", ZERO)

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


async def delete_reading(db: AsyncSession, reading_id: int):
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
    """
    from app.modules.utility.models import GSheetsImportRow
    from sqlalchemy import update

    res = await db.execute(
        select(MeterReading).where(MeterReading.id == reading_id)
    )
    reading = res.scalars().first()
    if not reading:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    # Отвязываем gsheets-строки, которые ссылались на это reading.
    # Без этого orphan-ссылки запутают админский UI и promote-задачу.
    await db.execute(
        update(GSheetsImportRow)
        .where(GSheetsImportRow.reading_id == reading_id)
        .values(reading_id=None, processed_at=None)
    )

    await db.delete(reading)
    await db.commit()
    return {"status": "deleted"}
