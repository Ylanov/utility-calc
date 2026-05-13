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
        fraction=Decimal("1")  # Доля прожитых дней в месяце (для выселения/переселения)
) -> dict:
    """
    Расчёт коммунальных платежей.

    ИСПРАВЛЕНИЯ по сравнению с предыдущей версией:
    1. Все вычисления выполняются на Decimal — нет погрешности float.
    2. Используется ROUND_HALF_UP вместо Python round() (банковское).
    3. safe_positive() применяется ко всем объёмам — отриц. объёмы = 0.
    4. total_cost = сумма Decimal-компонент — нет накопления float-ошибки.

    Формулы:
      ГВС      = объём_горячей * (тариф_подачи + тариф_нагрева)
      ХВС      = объём_холодной * тариф_подачи
      Канализ. = (ГВС + ХВС) объём * тариф_водоотведения
      Электро  = доля_кВт * тариф_электроэнергии
      Содержание = площадь * тариф * доля_дней
      Наём       = площадь * тариф * доля_дней
      ТКО        = площадь * тариф * доля_дней
      Фиксир.    = площадь * (тариф_отопления + ОДН_электро) * доля_дней
    """

    # ─────────────────────────────────────────────────
    # Если жилец на per_capita (холостяк, платит за койко-место) — счётчиков нет.
    # Делегируем calculate_per_capita и возвращаемся. Это защитная сетка:
    # вызывающий код может забыть проверить billing_mode и передать объёмы —
    # мы их игнорируем, потому что для одиночек это нерелевантно.
    # ─────────────────────────────────────────────────
    if getattr(user, "billing_mode", "by_meter") == "per_capita":
        return calculate_per_capita(user, tariff, fraction=fraction)

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
    v_sew  = safe_positive(D(volume_sewage))
    v_el   = safe_positive(D(volume_electricity_share))

    residents = D(user.residents_count if user.residents_count else 1)
    has_hw = getattr(user, "has_hw_meter", True)
    has_cw = getattr(user, "has_cw_meter", True)
    has_el = getattr(user, "has_el_meter", True)

    if not has_hw:
        # Счётчика ГВС нет — норматив × жильцов. Если norm=0 → 0.
        v_hot = safe_positive(D(getattr(tariff, "hw_norm_per_capita", 0)) * residents)
    if not has_cw:
        v_cold = safe_positive(D(getattr(tariff, "cw_norm_per_capita", 0)) * residents)
    if not has_el:
        v_el = safe_positive(D(getattr(tariff, "el_norm_per_capita", 0)) * residents)

    # Водоотведение тоже пересчитываем если хотя бы один из водных
    # счётчиков отсутствует — оно идёт от суммы (ГВС + ХВС).
    if (not has_hw) or (not has_cw):
        v_sew = v_hot + v_cold

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
    t_el_sqm = D(tariff.electricity_per_sqm)  # ОДН электроэнергия (на м²)

    # FAIL-LOUD: если ВСЕ тарифные поля = 0, расчёт лишён смысла. Это
    # либо отсутствующий тариф, либо некорректно созданный/неактивный.
    # Раньше функция тихо возвращала total_cost=0 — жилец видел «зеленую
    # квитанцию» на 0 руб, а бухгалтерия только через месяц обнаруживала
    # что начислений нет. Теперь явная ошибка на ранней стадии.
    all_rates = (t_w_sup, t_w_heat, t_sewage, t_el, t_maint, t_rent,
                 t_waste, t_heat, t_el_sqm)
    if all(rate == ZERO for rate in all_rates):
        raise CalculationError(
            "Тариф полностью пустой (все ставки = 0). Создайте/активируйте "
            "тариф через админку перед расчётом квитанций."
        )

    # ─────────────────────────────────────────────────
    # РАСЧЁТ ПО СЧЁТЧИКАМ
    # ─────────────────────────────────────────────────

    # ГВС: объём * (тариф подачи + тариф нагрева)
    c_hot = quantize_money(v_hot * (t_w_sup + t_w_heat))

    # ХВС: объём * тариф подачи
    c_cold = quantize_money(v_cold * t_w_sup)

    # Канализация: суммарный объём воды * тариф водоотведения
    c_sewage = quantize_money(v_sew * t_sewage)

    # Электроэнергия (доля жильца от расхода комнаты): кВт·ч * тариф
    c_elect = quantize_money(v_el * t_el)

    # ─────────────────────────────────────────────────
    # РАСЧЁТ ПО ПЛОЩАДИ (фиксированные начисления)
    # Умножаются на долю прожитых дней при частичном расчёте.
    # Площадь берётся из комнаты (не от пользователя) —
    # все фиксированные платежи начисляются на всю площадь помещения.
    # ─────────────────────────────────────────────────

    # Содержание и ремонт
    c_maint = quantize_money(area * t_maint * frac)

    # Социальный наём
    c_rent = quantize_money(area * t_rent * frac)

    # ТКО (мусор)
    c_waste = quantize_money(area * t_waste * frac)

    # Фиксированная часть: отопление + ОДН электроэнергия
    c_fixed = quantize_money(area * (t_heat + t_el_sqm) * frac)

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
