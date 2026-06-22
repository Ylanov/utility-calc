# app/modules/utility/services/calculations.py

import logging
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger("utility_calculations")

ZERO = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")


# Поля которые есть и в результате calculate_utilities, и в модели
# MeterReading. Используется в costs_for_model_fields() ниже — фильтрует
# sanity_warning (не поле БД), total_cost (caller сам решает: чистый
# total_cost из расчёта или grand_total с долгами и корректировками).
MODEL_COST_FIELDS = (
    "cost_hot_water",
    "cost_cold_water",
    "cost_sewage",
    "cost_electricity",
    "cost_maintenance",
    "cost_social_rent",
    "cost_waste",
    "cost_fixed_part",
)


def paying_residents(user, room) -> int:
    """Число людей жильца для расчёта (норматив бессчётчиковых + доля
    электричества).

    - ХОЛОСТЯЦКАЯ квартира: каждый жилец = 1 человек. Счётчики делятся на
      room.total_room_residents отдельно (calculate_utilities), поэтому здесь
      именно 1, иначе доля электричества схлопнулась бы в 1/1.
    - СЕМЬЯ: число людей семьи = User.residents_count лицевого счёта. Это поле
      БОЛЬШЕ НЕ редактируется в форме жильца (2026-06-17) — оно
      синхронизируется из Жилфонда (Room.total_room_residents) при создании
      жильца и правке комнаты. Биллинг семьи при этом НЕ меняется (читаем то же
      значение, что и раньше) — важно: НЕ переключать на total_room_residents,
      иначе доля электричества семьи скачком станет 100% (см. ревью 2026-06-17).

    Без комнаты/жильца → 1, чтобы расчёт не падал.
    """
    if room is not None and bool(getattr(room, "is_singles_apartment", False)):
        return 1
    rc = getattr(user, "residents_count", None) if user is not None else None
    return int(rc) if rc and int(rc) > 0 else 1


def resident_type_of(user, room) -> str:
    """Тип жильца ВЫВОДИТСЯ из КОМНАТЫ (единый источник, 2026-06-19): если
    комната холостяцкая (room.is_singles_apartment) → 'single', иначе 'family'.
    Раньше тип дублировался на User.resident_type и расходился с комнатой —
    отсюда сбои/лаги. User-поле оставлено дремлющим зеркалом (синхронизируется
    из комнаты при заселении), но ИСТИНА — комната."""
    return "single" if (room is not None and bool(getattr(room, "is_singles_apartment", False))) else "family"


def is_unconditional(tariff) -> bool:
    """Тариф «БЕЗ УСЛОВИЙ» (tariff_type='unconditional'): расход начисляется по
    НОРМАТИВУ НА КВАРТИРУ (фиксировано, не зависит от числа людей и счётчиков).
    Семья платит норму целиком; у холостяков она делится поровну (штатный
    singles-делёж в calculate_utilities). area-статьи (наём/ТКО/отопл/содерж)
    считаются по площади как обычно (по charge-флагам тарифа)."""
    return str(getattr(tariff, "tariff_type", "") or "").lower() == "unconditional"


def costs_for_model_fields(costs: dict) -> dict:
    """Возвращает подсловарь, безопасный для setattr/**kwargs на MeterReading.

    Раньше код напрямую делал `for k, v in costs.items(): setattr(...)`,
    и это работало пока все ключи calculate_utilities совпадали с полями
    модели. После добавления sanity_warning и потенциально других
    мета-полей — нужен фильтр. Этот helper централизует список.
    """
    return {k: costs[k] for k in MODEL_COST_FIELDS if k in costs}


class CalculationError(Exception):
    """Поднимается когда расчёт не может быть честно выполнен.

    Примеры: тариф полностью пустой (все ставки = 0); комната без площади
    при наличии фиксированных компонент; некорректные входные параметры.

    Раньше calculate_utilities тихо возвращал total_cost=0 в таких случаях —
    жилец видел «всё хорошо», бухгалтерия удивлялась через месяц. Теперь
    fail-loud: caller (mobile/admin) увидит 5xx и admin поймёт, что нужно
    настроить тариф / починить данные комнаты.
    """


def D(value) -> Decimal:
    """Безопасное приведение к Decimal."""
    if value is None:
        return ZERO
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def quantize_money(value: Decimal) -> Decimal:
    """
    Округление денежных значений до копеек.
    Используется ROUND_HALF_UP (стандартное математическое округление):
    0.005 → 0.01, 0.235 → 0.24, 0.245 → 0.25.

    Python built-in round() использует ROUND_HALF_EVEN (банковское):
    0.235 → 0.23 (неверно для ЖКХ-расчётов).
    """
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def safe_positive(value: Decimal) -> Decimal:
    """Защита от отрицательных объёмов — возвращает 0 если значение < 0."""
    return value if value > ZERO else ZERO


def calculate_per_capita(user, tariff, fraction=Decimal("1")) -> dict:
    """Расчёт для холостяков, оплачивающих койко-место.

    Это «плоская» сумма из тарифа per_capita_amount: счётчики у одиночек
    в одной квартире не разделяются индивидуально (физически невозможно),
    поэтому каждый холостяк платит фиксированную ставку, привязанную к
    тарифу его проживания.

    Возвращает ту же структуру что и calculate_utilities, чтобы вызывающий
    код мог не различать ветки. Все компоненты счётчиков = 0; вся сумма
    идёт в cost_fixed_part и в total_cost.
    """
    amount = quantize_money(D(getattr(tariff, "per_capita_amount", 0)) * D(fraction))
    return {
        "cost_hot_water":   ZERO,
        "cost_cold_water":  ZERO,
        "cost_sewage":      ZERO,
        "cost_electricity": ZERO,
        "cost_maintenance": ZERO,
        "cost_social_rent": ZERO,
        "cost_waste":       ZERO,
        "cost_fixed_part":  amount,   # вся сумма попадает в «фиксированную часть»
        "total_cost":       amount,
        "sanity_warning":   None,     # совместимость с calculate_utilities
    }


def calculate_utilities(
        user,
        room,
        tariff,
        volume_hot,
        volume_cold,
        volume_sewage,
        volume_electricity_share,
        fraction=Decimal("1"),  # Доля прожитых дней в месяце (для выселения/переселения)
        heating_season_active: bool = True,
        hot_water_heating_active: bool = True,
        sewage_correction=Decimal("0.000"),  # админ-корректировка объёма водоотведения
) -> dict:
    """
    Расчёт коммунальных платежей.

    ИСПРАВЛЕНИЯ по сравнению с предыдущей версией:
    1. Все вычисления выполняются на Decimal — нет погрешности float.
    2. Используется ROUND_HALF_UP вместо Python round() (банковское).
    3. safe_positive() применяется ко всем объёмам — отриц. объёмы = 0.
    4. total_cost = сумма Decimal-компонент — нет накопления float-ошибки.

    Формулы:
      ГВС      = объём_горячей * тариф_нагрева
      ХВС      = объём_холодной * тариф_подачи
      Канализ. = (ГВС + ХВС) объём * тариф_водоотведения
      Электро  = доля_кВт * тариф_электроэнергии
      Содержание = площадь_базовая * тариф * доля_дней
      Наём       = площадь_базовая * тариф * доля_дней
      ТКО        = площадь_базовая * тариф * доля_дней
      Отопление  = площадь_базовая * тариф_отопления * доля_дней

    площадь_базовая:
      family  — вся площадь помещения (один счёт на семью);
      singles — площадь / макс. вместимость квартиры (доля «проектного
                места»). Каждому холостяку начисляется ПОЛНОСТЬЮ, сколько
                бы фактически их ни жило. Пример: 43.1 м², макс. 4 →
                каждый платит за 10.775 м² × тариф.

    Счётчики у холостяков (singles) дополнительно делятся на ФАКТИЧЕСКОЕ
    число проживающих (room.total_room_residents, авто = COUNT жильцов).
    Электричество приходит уже как доля одного жильца (elect_share в
    обёртках billing) и повторно НЕ делится.

    ОДН (electricity_per_sqm) удалён из системы 29.05.2026 — поле в модели
    Tariff осталось для исторических квитанций, но в расчёте не участвует.
    """

    # ─────────────────────────────────────────────────
    # LEGACY per_capita УБРАН 2026-06-19. Раньше billing_mode='per_capita'
    # шортил в calculate_per_capita(tariff.per_capita_amount), а по политике
    # per_capita_amount=0 → счёт холостяка ОБНУЛЯЛСЯ, минуя корректную ветку
    # is_singles_apartment (делёж счётчиков). Финансовый импорт/gsheets ошибочно
    # ставили single→per_capita — отсюда «лаги/сбои расчёта». Теперь ВСЕ идут
    # по счётчиковому пути; холостяки делятся по room.is_singles_apartment.
    # ─────────────────────────────────────────────────
    # Объёмы: приводим к Decimal, защищаем от отрицательных значений.
    # Отрицательный объём физически невозможен и должен давать 0, а не
    # отрицательную сумму в квитанции.
    #
    # NB: если у жильца НЕТ счётчика конкретного ресурса (has_X_meter=False),
    # передан volume=0. В этом случае используем НОРМАТИВ из тарифа
    # (X_norm_per_capita × residents_count). Это правильное поведение
    # для жильцов без счётчика — иначе им бы ничего не начислялось за
    # ресурсы которые они потребляют.
    # ─────────────────────────────────────────────────
    v_hot  = safe_positive(D(volume_hot))
    v_cold = safe_positive(D(volume_cold))
    v_el   = safe_positive(D(volume_electricity_share))
    # v_sew (водоотведение) считаем ниже, ПОСЛЕ charge-флагов — как сумму
    # только заряжаемых водных объёмов (Аудит #4). Переданный volume_sewage
    # (= hot+cold) — производное, здесь не используем.

    # Число людей для норматива бессчётчиковых — из КОМНАТЫ (paying_residents),
    # не из упразднённого User.residents_count.
    residents = D(paying_residents(user, room))
    # Наличие счётчиков — приоритет КОМНАТЫ (статично, meters_002), fallback
    # на User для совместимости со старыми данными до переноса.
    def _has_meter(attr: str) -> bool:
        rv = getattr(room, attr, None)
        return bool(rv) if rv is not None else bool(getattr(user, attr, True))
    has_hw = _has_meter("has_hw_meter")
    has_cw = _has_meter("has_cw_meter")
    has_el = _has_meter("has_el_meter")

    # Тариф «БЕЗ УСЛОВИЙ» (unconditional): объёмы УЖЕ переданы caller'ом как
    # норматив НА КВАРТИРУ (compute_reading_breakdown), счётчики игнорируем —
    # не подменяем их нормативом×жильцов (это был бы вариант «на человека»).
    # Делёж между холостяками выполнит singles-блок ниже.
    if not is_unconditional(tariff):
        if not has_hw:
            # Счётчика ГВС нет — норматив × жильцов. Если norm=0 → 0.
            v_hot = safe_positive(D(getattr(tariff, "hw_norm_per_capita", 0)) * residents)
        if not has_cw:
            v_cold = safe_positive(D(getattr(tariff, "cw_norm_per_capita", 0)) * residents)
        if not has_el:
            v_el = safe_positive(D(getattr(tariff, "el_norm_per_capita", 0)) * residents)

    # Площадь комнаты
    area = D(room.apartment_area or 0)

    # Доля дней (1 для полного месяца, дробь для выселения)
    frac = D(fraction)
    if frac <= ZERO or frac > Decimal("1"):
        frac = Decimal("1")

    # ─────────────────────────────────────────────────
    # Тарифы — сразу Decimal, без конвертации через float
    # ─────────────────────────────────────────────────
    t_w_sup  = D(tariff.water_supply)     # подача воды (ГВС + ХВС)
    t_w_heat = D(tariff.water_heating)    # нагрев воды (только ГВС)
    t_sewage = D(tariff.sewage)           # водоотведение
    t_el     = D(tariff.electricity_rate) # электроэнергия (кВт·ч)
    t_maint  = D(tariff.maintenance_repair) # содержание и ремонт
    t_rent   = D(tariff.social_rent)      # социальный наём
    t_waste  = D(tariff.waste_disposal)   # ТКО (мусор)
    t_heat   = D(tariff.heating)          # отопление (на м²)

    # FAIL-LOUD: если ВСЕ тарифные поля = 0, расчёт лишён смысла. Это
    # либо отсутствующий тариф, либо некорректно созданный/неактивный.
    # Раньше функция тихо возвращала total_cost=0 — жилец видел «зеленую
    # квитанцию» на 0 руб, а бухгалтерия только через месяц обнаруживала
    # что начислений нет. Теперь явная ошибка на ранней стадии.
    all_rates = (t_w_sup, t_w_heat, t_sewage, t_el, t_maint, t_rent,
                 t_waste, t_heat)
    if all(rate == ZERO for rate in all_rates):
        raise CalculationError(
            "Тариф полностью пустой (все ставки = 0). Создайте/активируйте "
            "тариф через админку перед расчётом квитанций."
        )

    # Сезонные переключатели (управляются админом в Операциях → Сезоны).
    # При выключенном отопительном сезоне cost_fixed_part не включает heating
    # для всех жильцов сразу. Аналогично — подогрев ГВС на летнюю
    # профилактику ТЭЦ: при выключенном hot_water_heating_active
    # переключаемся на water_supply (см. ниже формулу c_hot).
    # Применяем после FAIL-LOUD-проверки чтобы не падать на тарифе где
    # только heating ненулевой, при выключенном сезоне.
    if not heating_season_active:
        t_heat = ZERO
    # ─────────────────────────────────────────────────
    # РАСЧЁТ ПО СЧЁТЧИКАМ
    # ─────────────────────────────────────────────────
    # Bug AT: «charge_*»-флаги тарифа — глобально что начисляется.
    # Для legacy-тарифов (где поля ещё None из БД) считаем как True
    # через getattr с default.
    def _charge(field: str) -> bool:
        v = getattr(tariff, field, None)
        return True if v is None else bool(v)

    # Водоотведение = объём ТОЛЬКО заряжаемых водных ресурсов (ГВС+ХВС) за
    # вычетом админ-корректировки sewage_correction.
    # Аудит #4: при charge_*=False объём ресурса НЕ должен течь в водоотведение
    # (иначе жилец платит за воду, которую тариф не начисляет). v_hot/v_cold уже
    # учли норматив при отсутствии счётчика.
    # Регресс-фикс (ревизия): sewage_correction раньше зашивался caller'ом в
    # volume_sewage; новая формула его игнорировала → корректировка терялась,
    # водоотведение завышалось. Теперь корректировка — явный параметр, вычитается
    # здесь (clamp ≥0). При обоих заряжаемых и corr=0 результат = прежний
    # (v_hot+v_cold) — без регрессии.
    v_sew = safe_positive(
        (v_hot if _charge("charge_hot_water") else ZERO)
        + (v_cold if _charge("charge_cold_water") else ZERO)
        - D(sewage_correction)
    )

    # ГВС (Bug AP, 2026-05): тариф water_heating уже включает в себя
    # стоимость воды + подогрева — это единая цена 1 м³ ГВС.
    # Поэтому НЕ суммируем с water_supply. Раньше формула была
    # vol × (water_supply + water_heating) — стандарт ЖКХ когда
    # water_heating означает «только подогрев», но в этом проекте
    # бизнес-логика другая. См. memory/tariff_hw_pricing.md.
    #
    # Летняя профилактика ТЭЦ: при hot_water_heating_active=False
    # подогрева нет → жилец платит как за ХВС (water_supply).
    if not _charge("charge_hot_water"):
        c_hot = ZERO
    elif hot_water_heating_active:
        c_hot = quantize_money(v_hot * t_w_heat)
    else:
        c_hot = quantize_money(v_hot * t_w_sup)

    # ХВС: объём * тариф подачи
    c_cold = ZERO if not _charge("charge_cold_water") else quantize_money(v_cold * t_w_sup)

    # Канализация: суммарный объём воды * тариф водоотведения
    c_sewage = ZERO if not _charge("charge_sewage") else quantize_money(v_sew * t_sewage)

    # Электроэнергия (доля жильца от расхода комнаты): кВт·ч * тариф
    c_elect = ZERO if not _charge("charge_electricity") else quantize_money(v_el * t_el)

    # ─────────────────────────────────────────────────
    # РАСЧЁТ ПО ПЛОЩАДИ (фиксированные начисления)
    # Умножаются на долю прожитых дней при частичном расчёте.
    # Площадь берётся из комнаты (не от пользователя) —
    # все фиксированные платежи начисляются на всю площадь помещения.
    # ─────────────────────────────────────────────────

    # singles-режим триггерится ТОЛЬКО через `room.is_singles_apartment=True`
    # — статус «холостяцкой квартиры» это атрибут КОМНАТЫ, не тарифа.
    # Bug AT этап 3: ПЕРЕД skip — проверяем глобальный charge-флаг.
    # charge_X=False → c_X=0 для всех (не только холостяков).
    is_singles_apt = bool(getattr(room, "is_singles_apartment", False))

    # База площади для area-based статей (содержание/наём/ТКО/отопление).
    #
    # family:  вся площадь помещения — один счёт на семью.
    # singles: «проектное место» = площадь / макс. вместимость квартиры.
    #          Каждый холостяк платит за свою долю площади ПОЛНОСТЬЮ — эти
    #          статьи НЕ делятся между фактически живущими (в отличие от
    #          счётчиков ниже). Пример: 43.1 м², макс. вместимость 4 →
    #          каждый платит за 10.775 м² × тариф, сколько бы их ни жило.
    #          max_capacity обязателен для холостяцкой квартиры (валидируется
    #          при сохранении комнаты); fallback на факт. число жильцов —
    #          защита от старых данных без проставленной вместимости.
    if is_singles_apt:
        _max_cap = getattr(room, "max_capacity", None)
        if _max_cap and int(_max_cap) > 0:
            cap = D(_max_cap)
        else:
            cap = D(getattr(room, "total_room_residents", None) or 1)
        if cap <= ZERO:
            cap = Decimal("1")
        area_base = area / cap
    else:
        area_base = area

    # Содержание и ремонт
    if not _charge("charge_maintenance"):
        c_maint = ZERO
    elif is_singles_apt and bool(getattr(tariff, "singles_skip_maintenance", False)):
        c_maint = ZERO
    else:
        c_maint = quantize_money(area_base * t_maint * frac)

    # Социальный наём
    if not _charge("charge_social_rent"):
        c_rent = ZERO
    elif is_singles_apt and bool(getattr(tariff, "singles_skip_social_rent", False)):
        c_rent = ZERO
    else:
        c_rent = quantize_money(area_base * t_rent * frac)

    # ТКО (мусор)
    if not _charge("charge_waste"):
        c_waste = ZERO
    elif is_singles_apt and bool(getattr(tariff, "singles_skip_waste", False)):
        c_waste = ZERO
    else:
        c_waste = quantize_money(area_base * t_waste * frac)

    # Отопление (фиксированная часть). ОДН удалён из системы 29.05.2026 —
    # раньше было area × (heating + electricity_per_sqm), теперь только
    # отопление. Для холостяков отключается через singles_skip_heating;
    # глобально — через charge_heating.
    if not _charge("charge_heating"):
        _t_heat_effective = ZERO
    elif is_singles_apt and bool(getattr(tariff, "singles_skip_heating", False)):
        _t_heat_effective = ZERO
    else:
        _t_heat_effective = t_heat
    c_fixed = quantize_money(area_base * _t_heat_effective * frac)

    # Деление СЧЁТЧИКОВ между фактически проживающими холостяками.
    # ГВС/ХВС/канализация приходят полным объёмом по квартире → делим на
    # факт. число жильцов (room.total_room_residents, авто = COUNT жильцов).
    # Электричество НЕ делим: оно уже пришло как доля одного жильца
    # (elect_share = объём / total_room_residents в обёртках billing) —
    # повторное деление дало бы /N². area-based статьи (выше) тоже НЕ
    # делятся — они уже посчитаны по «проектному месту».
    if is_singles_apt:
        n_fact = D(getattr(room, "total_room_residents", None) or 1)
        if n_fact > ZERO:
            c_hot = quantize_money(c_hot / n_fact)
            c_cold = quantize_money(c_cold / n_fact)
            c_sewage = quantize_money(c_sewage / n_fact)

    # ─────────────────────────────────────────────────
    # ИТОГ
    # Суммируем Decimal-значения — без накопления float-погрешности.
    # Дополнительное quantize гарантирует ровно 2 знака.
    # ─────────────────────────────────────────────────
    total_cost = quantize_money(
        c_hot + c_cold + c_sewage + c_elect + c_maint + c_rent + c_waste + c_fixed
    )

    # SANITY-WARNING (не блокирующее): если итог явно аномален — жилец
    # увидит «необычно высокий счёт» в UI. Не raise, потому что:
    #   - редкие легитимные кейсы возможны (большая семья, переезд после
    #     долгого отсутствия, накопленный долг);
    #   - блокирующий error на этом уровне уже даёт validate_meter_reading
    #     по входным значениям и validate_total_cost по выходному.
    # Single source of truth — порог берётся из analyzer_config через
    # тот же getter, что использует validate_total_cost. Раньше тут была
    # отдельная константа MAX_TOTAL_COST_PER_READING — рассинхрон при
    # изменении настроек админом.
    from app.modules.utility.services.reading_validators import (
        get_max_total_cost_per_reading,
    )
    ceiling = get_max_total_cost_per_reading()
    sanity_warning = None
    if total_cost > ceiling:
        sanity_warning = (
            f"Итоговая сумма {total_cost} ₽ необычно высока для типичного "
            f"месяца (порог {ceiling} ₽). Проверьте "
            f"показания счётчиков и тариф."
        )
        logger.warning(
            "[CALC-SANITY] total_cost=%s > %s for area=%s, volumes "
            "hot=%s cold=%s sewage=%s elect=%s",
            total_cost, ceiling, area,
            v_hot, v_cold, v_sew, v_el,
        )

    return {
        "cost_hot_water":   c_hot,
        "cost_cold_water":  c_cold,
        "cost_sewage":      c_sewage,
        "cost_electricity": c_elect,
        "cost_maintenance": c_maint,
        "cost_social_rent": c_rent,
        "cost_waste":       c_waste,
        "cost_fixed_part":  c_fixed,
        "total_cost":       total_cost,
        "sanity_warning":   sanity_warning,
    }
