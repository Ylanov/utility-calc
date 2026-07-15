# EXPLAIN — детальный пересчёт одного reading с трассировкой умножений
# («Проверить расчёт» в админ-UI). Вербатим-перенос из admin_reports.py
# (строки 2055-2509), поведение 1:1.

from decimal import Decimal

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.utility.models import User, MeterReading, Tariff, Adjustment

from ._shared import ZERO, logger, router


# =====================================================================
# EXPLAIN — детальный пересчёт одного reading с трассировкой умножений
# =====================================================================
@router.get("/api/admin/readings/{reading_id}/explain")
async def explain_reading_calculation(
    reading_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает детальный breakdown расчёта одного MeterReading.

    Цель: админ может убедиться что счёт выставлен ПРАВИЛЬНО — видит
    каждое умножение тариф×объём, какие были предыдущие показания, какие
    корректировки применены, и совпадает ли пересчитанный итог с тем,
    что лежит в БД. Если не совпадает — пересчёт прав, в БД мусор.

    Используется кнопкой «Проверить расчёт» в админ-UI рядом с reading.
    """
    if current_user.role not in ("accountant", "admin", "financier"):
        raise HTTPException(403, "Доступ запрещён")

    # Весь основной блок завернём в try/except — если что-то падает на
    # подзадаче (битый тариф, отсутствующий period, etc), вернём 200 с
    # ключом explain_error вместо 500. UI тогда покажет красную плашку
    # с объяснением вместо обвинения «ошибка загрузки».
    try:
        return await _build_explain_response(reading_id, db)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"explain failed for reading_id={reading_id}: {e}")
        return {
            "explain_error": f"{type(e).__name__}: {e}",
            "reading_id": reading_id,
        }


async def _build_explain_response(reading_id: int, db: AsyncSession) -> dict:
    # 1. Reading с user, room, period
    reading = (await db.execute(
        select(MeterReading)
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
            selectinload(MeterReading.period),
        )
        .where(MeterReading.id == reading_id)
    )).scalars().first()
    if not reading:
        raise HTTPException(404, "Reading не найден")

    user = reading.user
    room = user.room if user else None
    if not user or not room:
        raise HTTPException(400, "У reading'а нет связанного жильца или комнаты")

    # 2. Тариф через тот же кеш что использовался при расчёте
    from app.modules.utility.services.tariff_cache import tariff_cache
    tariff = tariff_cache.get_effective_tariff(user=user, room=room)
    if not tariff:
        tariff = (await db.execute(
            select(Tariff).where(Tariff.is_active.is_(True))
        )).scalars().first()
    if not tariff:
        raise HTTPException(400, "Активный тариф не найден")

    # 3. Предыдущее utverждённое показание для дельт. Ищем по period_id
    # (не created_at — инцидент may 2026 с подачами заднего числа через
    # гугл-таблицу). И ПРОПУСКАЕМ synth-prev (AUTO_GENERATED, DATA_OVERFLOW_RESET,
    # MANUAL_RECEIPT, AUTO_NO_HISTORY) — их значения = 0, использование как
    # baseline даёт фантастическую дельту в следующем периоде.
    # См. инцидент Капранов 2026-05-21: prev=AUTO_GENERATED с 0/0/0, текущее
    # 1468 ГВС → формула выдавала 818 049 ₽ как «правильный пересчёт».
    # selectinload(period) — чтобы prev.period.name не дёргал lazy-load в async.
    from app.modules.utility.services.reading_calculator import is_meaningful_prev
    prev_candidates = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.period))
        .where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved.is_(True),
            MeterReading.period_id < reading.period_id,
        )
        .order_by(MeterReading.period_id.desc())
        .limit(20)
    )).scalars().all()
    prev = next((c for c in prev_candidates if is_meaningful_prev(c)), None)

    # 4. Корректировки за период reading'а
    adj_rows = (await db.execute(
        select(Adjustment).where(
            Adjustment.user_id == user.id,
            Adjustment.period_id == reading.period_id,
        )
    )).scalars().all()

    # 5. Считаем дельты — те же формулы что в client_readings.py:
    #    delta_hot = current - prev (или current если prev=None)
    #    delta_sewage = delta_hot + delta_cold
    #    delta_elect_share = (residents/total_room) × delta_elect
    z = Decimal("0")
    cur_hot = Decimal(str(reading.hot_water or 0))
    cur_cold = Decimal(str(reading.cold_water or 0))
    cur_elect = Decimal(str(reading.electricity or 0))
    p_hot = Decimal(str(prev.hot_water or 0)) if prev else z
    p_cold = Decimal(str(prev.cold_water or 0)) if prev else z
    p_elect = Decimal(str(prev.electricity or 0)) if prev else z

    d_hot = cur_hot - p_hot
    d_cold = cur_cold - p_cold
    d_elect = cur_elect - p_elect
    d_sewage = d_hot + d_cold

    from app.modules.utility.services.calculations import paying_residents
    residents = Decimal(paying_residents(user, room))
    total_room = Decimal(room.total_room_residents or 1)
    if total_room <= 0:
        total_room = Decimal("1")
    elect_share = (residents / total_room) * d_elect

    area = Decimal(str(room.apartment_area or 0))

    # Тариф «БЕЗ УСЛОВИЙ»: расход = НОРМАТИВ на квартиру (не дельта счётчиков).
    # Переопределяем объёмы как в reading_calculator.compute_reading_breakdown,
    # чтобы «Проверка расчёта» совпадала с реальным биллингом, а не показывала
    # ложный «baseline 0 / потребление не начисляется».
    from app.modules.utility.services.calculations import is_unconditional as _is_uncond
    is_unconditional_tariff = _is_uncond(tariff)
    if is_unconditional_tariff:
        _uncond_singles = bool(getattr(room, "is_singles_apartment", False))
        d_hot = Decimal(str(getattr(tariff, "hw_norm_per_capita", 0) or 0))
        d_cold = Decimal(str(getattr(tariff, "cw_norm_per_capita", 0) or 0))
        d_sewage = d_hot + d_cold
        _el_norm = Decimal(str(getattr(tariff, "el_norm_per_capita", 0) or 0))
        elect_share = ((residents / total_room) * _el_norm) if _uncond_singles else _el_norm

    # 6. Пересчёт через ту же calculate_utilities (для сравнения с БД).
    # Пробуем; если падает на CalculationError — показываем причину явно.
    from app.modules.utility.services.calculations import (
        calculate_utilities, CalculationError
    )
    calc_error = None
    calc_result = None
    # Для «без условий» это НЕ baseline (расход по нормативу начисляется).
    is_baseline = (prev is None) and not is_unconditional_tariff
    # Сезонные флаги — «Проверить расчёт» обязан использовать тот же набор,
    # что и реальный /api/calculate: global + per-tariff.
    from app.modules.utility.routers.settings import _load_seasonal
    _seasonal = await _load_seasonal(db)
    _heating = _seasonal.heating_season_active and tariff.is_heating_active_now()
    _hw = _seasonal.hot_water_heating_active and tariff.is_hw_heating_active_now()
    try:
        # BASELINE: потребление-зависимые статьи (вода/свет) = 0 (счётчик
        # «накручен», дельту от 0 брать нельзя), НО area-based (содержание/
        # наём/ТКО/отопление) ПЛАТЯТСЯ ВСЕГДА (Bug L — см.
        # reading_calculator.compute_reading_breakdown, инцидент Резунов 04.2026).
        # Раньше тут был ХАРДКОД всех нулей → «Проверка расчёта» показывала
        # ЛОЖНОЕ расхождение на КАЖДОМ baseline (формула 0 vs БД area-based
        # ~5000-7000 ₽). Реальный биллинг и батч-анализатор периода считают
        # baseline правильно (area-based) — теперь и эта модалка тоже:
        # calculate_utilities с volume_*=0 для baseline.
        calc_result = calculate_utilities(
            user=user, room=room, tariff=tariff,
            volume_hot=(z if is_baseline else d_hot),
            volume_cold=(z if is_baseline else d_cold),
            volume_sewage=(z if is_baseline else d_sewage),
            volume_electricity_share=(z if is_baseline else elect_share),
            heating_season_active=_heating,
            hot_water_heating_active=_hw,
        )
    except CalculationError as e:
        calc_error = str(e)

    # 7. Формируем breakdown — по компонентам, с явными формулами.
    # Null-safe: tariff-поля могут быть None для редких/тестовых тарифов,
    # без guard это даёт Decimal('None') → InvalidOperation → 500.
    def f(d):
        """Decimal/None/число → строка с 2 знаками для ₽-сумм."""
        if d is None:
            return "0.00"
        try:
            return f"{Decimal(str(d)):.2f}"
        except Exception:
            return str(d)

    def f3(d):
        """Decimal/None/число → строка с 3 знаками для м³/кВт·ч."""
        if d is None:
            return "0.000"
        try:
            return f"{Decimal(str(d)):.3f}"
        except Exception:
            return str(d)

    def _dec_or_zero(value):
        """Безопасный Decimal: None и невалидные → ZERO."""
        if value is None:
            return ZERO
        try:
            return Decimal(str(value))
        except Exception:
            return ZERO

    components = []
    if not is_baseline and calc_result:
        # Холостяцкая квартира: счётчики (ГВС/ХВС/канализация) делятся на факт.
        # число жильцов, а area-based статьи считаются от «проектного места»
        # area / max_capacity. Здесь готовим текст-объяснение; сами суммы —
        # из calc_result (= calculate_utilities). См. её docstring.
        _is_singles = bool(getattr(room, "is_singles_apartment", False))
        if _is_singles:
            _cap = room.max_capacity if (room.max_capacity and int(room.max_capacity) > 0) else (room.total_room_residents or 1)
            _area_base = area / Decimal(str(_cap or 1))
            _area_expr = f"({f(area)} / {_cap})"
            _meter_div = f" ÷ {total_room}" if (total_room and int(total_room) > 1) else ""
            _meter_note = " ÷ жильцов" if _meter_div else ""
        else:
            _area_base = area
            _area_expr = f(area)
            _meter_div = ""
            _meter_note = ""
        # ГВС (Bug AQ/AR): формула обновлена под новую бизнес-логику —
        # water_heating уже включает в себя стоимость воды + подогрева,
        # поэтому НЕ суммируем с water_supply. Летняя профилактика
        # (hot_water_heating_active=False) — переключение на water_supply.
        t_w_sup = _dec_or_zero(tariff.water_supply)
        t_w_heat = _dec_or_zero(tariff.water_heating)
        if _hw:
            hot_formula = "v_hot × water_heating" + _meter_note
            hot_calc = f"{f3(d_hot)} × {f(t_w_heat)}{_meter_div}"
        else:
            hot_formula = "v_hot × water_supply (профилактика ГВС)" + _meter_note
            hot_calc = f"{f3(d_hot)} × {f(t_w_sup)}{_meter_div}"
        components.append({
            "label": "Горячая вода",
            "kbk": "209",
            "formula": hot_formula,
            "calculation": hot_calc,
            "result": f(calc_result["cost_hot_water"]) + " ₽",
        })
        # ХВС
        components.append({
            "label": "Холодная вода",
            "kbk": "209",
            "formula": "v_cold × water_supply" + _meter_note,
            "calculation": f"{f3(d_cold)} × {f(t_w_sup)}{_meter_div}",
            "result": f(calc_result["cost_cold_water"]) + " ₽",
        })
        # Канализация
        t_sewage = _dec_or_zero(tariff.sewage)
        components.append({
            "label": "Водоотведение",
            "kbk": "209",
            "formula": "(v_hot + v_cold) × sewage_rate" + _meter_note,
            "calculation": f"{f3(d_sewage)} × {f(t_sewage)}{_meter_div}",
            "result": f(calc_result["cost_sewage"]) + " ₽",
        })
        # Электро
        t_el = _dec_or_zero(tariff.electricity_rate)
        components.append({
            "label": "Электроэнергия",
            "kbk": "209",
            "formula": "(жильцов / всех в комнате) × delta_elect × rate",
            "calculation": (
                f"({residents} / {total_room}) × {f3(d_elect)} × "
                f"{f(t_el)} = {f3(elect_share)} × {f(t_el)}"
            ),
            "result": f(calc_result["cost_electricity"]) + " ₽",
        })
        # area-based статьи: для семьи база = вся площадь, для холостяка =
        # area / max_capacity («проектное место»), НЕ делится на жильцов.
        # Содержание
        t_maint = _dec_or_zero(tariff.maintenance_repair)
        components.append({
            "label": "Содержание и ремонт",
            "kbk": "205",
            "formula": "area_base × maintenance_repair",
            "calculation": f"{_area_expr} × {f(t_maint)}",
            "result": f(calc_result["cost_maintenance"]) + " ₽",
        })
        # Наём
        t_rent = _dec_or_zero(tariff.social_rent)
        components.append({
            "label": "Социальный найм",
            "kbk": "205",
            "formula": "area_base × social_rent",
            "calculation": f"{_area_expr} × {f(t_rent)}",
            "result": f(calc_result["cost_social_rent"]) + " ₽",
        })
        # ТКО
        t_waste = _dec_or_zero(tariff.waste_disposal)
        components.append({
            "label": "Вывоз ТКО",
            "kbk": "205",
            "formula": "area_base × waste_disposal",
            "calculation": f"{_area_expr} × {f(t_waste)}",
            "result": f(calc_result["cost_waste"]) + " ₽",
        })
        # Отопление. ОДН (electricity_per_sqm) удалён из системы 29.05.2026 —
        # фиксированная часть = только отопление.
        t_h = _dec_or_zero(tariff.heating)
        components.append({
            "label": "Отопление",
            "kbk": "205",
            "formula": "area_base × heating",
            "calculation": f"{_area_expr} × {f(t_h)}",
            "result": f(calc_result["cost_fixed_part"]) + " ₽",
        })

    # 8. Сравнение пересчитанного с тем что в БД.
    #
    # ВАЖНО (инцидент may 2026 — «Капранов 818k → 0»):
    # calc_total — это только НАЧИСЛЕНИЯ за период (cost_*).
    # reading.total_cost — это total_209 + total_205, где total_209 =
    # cost_209 + debt_209 - overpayment_209 + adjustments. То есть он
    # ВКЛЮЧАЕТ долги перенесённые из прошлого периода.
    #
    # Раньше сравнивали calc_total с total_cost напрямую — у baseline
    # с долгом 7 323 ₽ это давало «РАСХОЖДЕНИЕ -7 323». Ложная тревога.
    # Теперь сравниваем calc_total с СУММОЙ cost_* полей (= чистые
    # начисления без долгов). Если они равны → БД актуальна,
    # даже если total_cost ≠ calc_total из-за переноса долга.
    def _dec(x):
        return Decimal(str(x or 0))
    stored_total = _dec(reading.total_cost)
    stored_cost_pure = (
        _dec(reading.cost_hot_water)
        + _dec(reading.cost_cold_water)
        + _dec(reading.cost_sewage)
        + _dec(reading.cost_electricity)
        + _dec(reading.cost_maintenance)
        + _dec(reading.cost_social_rent)
        + _dec(reading.cost_waste)
        + _dec(reading.cost_fixed_part)
    )
    calc_total = (
        Decimal(str(calc_result["total_cost"])) if calc_result else None
    )
    # match по чистым начислениям — это и есть «формула актуальна?»
    match = (
        calc_total is not None
        and abs(calc_total - stored_cost_pure) < Decimal("0.02")
    )
    # Разница между total_cost и stored_cost_pure = долги/переплаты/коррекции,
    # перенесённые из прошлых периодов. Не баг расчёта.
    carried_balance = stored_total - stored_cost_pure

    return {
        "reading": {
            "id": reading.id,
            "is_approved": bool(reading.is_approved),
            "anomaly_flags": reading.anomaly_flags,
            "anomaly_score": reading.anomaly_score,
            "created_at": reading.created_at.isoformat() if reading.created_at else None,
            "is_baseline": is_baseline,
            "is_unconditional": is_unconditional_tariff,
        },
        "user": {
            "id": user.id,
            "username": user.username,
            "residents_count": room.total_room_residents or 1,
            "billing_mode": getattr(user, "billing_mode", "by_meter"),
        },
        "room": {
            "id": room.id,
            "dormitory_name": room.dormitory_name,
            "room_number": room.room_number,
            "apartment_area": f(area),
            "total_room_residents": room.total_room_residents or 1,
        },
        "period": {"id": reading.period_id, "name": reading.period.name if reading.period else None},
        "tariff": {
            "id": tariff.id,
            "name": tariff.name,
            "rates": {
                "water_supply": f(tariff.water_supply),
                "water_heating": f(tariff.water_heating),
                "sewage": f(tariff.sewage),
                "electricity_rate": f(tariff.electricity_rate),
                "maintenance_repair": f(tariff.maintenance_repair),
                "social_rent": f(tariff.social_rent),
                "waste_disposal": f(tariff.waste_disposal),
                "heating": f(tariff.heating),
            },
            "norms": {
                "hw_norm": f3(getattr(tariff, "hw_norm_per_capita", 0)),
                "cw_norm": f3(getattr(tariff, "cw_norm_per_capita", 0)),
                "el_norm": f3(getattr(tariff, "el_norm_per_capita", 0)),
            },
        },
        "previous_reading": (
            {
                "reading_id": prev.id,
                "period_name": prev.period.name if prev.period else None,
                "hot_water": f3(p_hot),
                "cold_water": f3(p_cold),
                "electricity": f3(p_elect),
                "created_at": prev.created_at.isoformat() if prev.created_at else None,
            } if prev else None
        ),
        "current_values": {
            "hot_water": f3(cur_hot),
            "cold_water": f3(cur_cold),
            "electricity": f3(cur_elect),
        },
        "deltas": {
            "hot_water": f3(d_hot),
            "cold_water": f3(d_cold),
            "electricity": f3(d_elect),
            "electricity_share": f3(elect_share),
            "sewage": f3(d_sewage),
        },
        "components": components,
        "adjustments": [
            {
                "id": a.id,
                "amount": f(a.amount),
                "description": a.description,
                "kbk": a.account_type,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in adj_rows
        ],
        "balances_carried_in": {
            "debt_209": f(reading.debt_209 or 0),
            "overpayment_209": f(reading.overpayment_209 or 0),
            "debt_205": f(reading.debt_205 or 0),
            "overpayment_205": f(reading.overpayment_205 or 0),
        },
        "totals": {
            "calculated_total_cost": f(calc_total) if calc_total is not None else None,
            "stored_total_cost": f(stored_total),
            "stored_cost_pure": f(stored_cost_pure),
            "stored_total_209": f(reading.total_209 or 0),
            "stored_total_205": f(reading.total_205 or 0),
            # match сравнивает calc с чистыми начислениями (без долгов).
            # Если true — формула актуальна. Если false — реально надо пересчитать.
            "match": match,
            # Разница между total_cost и чистыми cost — это переносы баланса
            # (debt_209/205, переплаты, корректировки). НЕ баг расчёта.
            "carried_balance": f(carried_balance),
            # Старое поле для обратной совместимости фронта. Не использовать
            # для match-логики — используйте diff_calc_minus_pure_cost.
            "diff_calc_minus_stored": (
                f(calc_total - stored_total) if calc_total is not None else None
            ),
            "diff_calc_minus_pure_cost": (
                f(calc_total - stored_cost_pure) if calc_total is not None else None
            ),
        },
        "sanity_warning": (
            calc_result.get("sanity_warning") if calc_result else None
        ),
        "calculation_error": calc_error,
    }
