# app/modules/utility/services/calculations.py

import logging
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger("utility_calculations")

ZERO = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")


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
    # Объёмы: приводим к Decimal, защищаем от отрицательных значений.
    # Отрицательный объём физически невозможен и должен давать 0, а не
    # отрицательную сумму в квитанции.
    # ─────────────────────────────────────────────────
    v_hot  = safe_positive(D(volume_hot))
    v_cold = safe_positive(D(volume_cold))
    v_sew  = safe_positive(D(volume_sewage))
    v_el   = safe_positive(D(volume_electricity_share))

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
    }