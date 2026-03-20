import logging
from decimal import Decimal, ROUND_HALF_UP
from app.modules.utility.models import User, Tariff


logger = logging.getLogger("utility_calculations")


ZERO = Decimal("0.00")
MONEY_QUANT = Decimal("0.01")


def D(value) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(value)


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def safe_positive(value: Decimal) -> Decimal:
    return value if value > ZERO else ZERO


def calculate_utilities(
        user, tariff, volume_hot, volume_cold, volume_sewage, volume_electricity_share, fraction=1.0
) -> dict:
    # 1. Переводим всё в аппаратный float (в 50 раз быстрее Decimal)
    v_hot = float(volume_hot) if float(volume_hot) > 0 else 0.0
    v_cold = float(volume_cold) if float(volume_cold) > 0 else 0.0
    v_sew = float(volume_sewage) if float(volume_sewage) > 0 else 0.0
    v_el = float(volume_electricity_share) if float(volume_electricity_share) > 0 else 0.0

    area = float(user.apartment_area or 0.0)
    frac = float(fraction)

    t_w_sup = float(tariff.water_supply)
    t_w_heat = float(tariff.water_heating)
    t_sewage = float(tariff.sewage)
    t_el_rate = float(tariff.electricity_rate)
    t_maint = float(tariff.maintenance_repair)
    t_rent = float(tariff.social_rent)
    t_waste = float(tariff.waste_disposal)
    t_heat = float(tariff.heating)
    t_el_sqm = float(tariff.electricity_per_sqm)

    # 2. Быстрая математика
    c_hot = round(v_hot * (t_w_sup + t_w_heat), 2)
    c_cold = round(v_cold * t_w_sup, 2)
    c_sewage = round(v_sew * t_sewage, 2)
    c_elect = round(v_el * t_el_rate, 2)

    c_maint = round(area * t_maint * frac, 2)
    c_rent = round(area * t_rent * frac, 2)
    c_waste = round(area * t_waste * frac, 2)
    c_fixed = round(area * (t_heat + t_el_sqm) * frac, 2)

    total = c_hot + c_cold + c_sewage + c_elect + c_maint + c_rent + c_waste + c_fixed

    # 3. Возвращаем Decimal ТОЛЬКО для записи в БД (чтобы SQLAlchemy не ругалась)
    return {
        "cost_hot_water": Decimal(str(c_hot)),
        "cost_cold_water": Decimal(str(c_cold)),
        "cost_sewage": Decimal(str(c_sewage)),
        "cost_electricity": Decimal(str(c_elect)),
        "cost_maintenance": Decimal(str(c_maint)),
        "cost_social_rent": Decimal(str(c_rent)),
        "cost_waste": Decimal(str(c_waste)),
        "cost_fixed_part": Decimal(str(c_fixed)),
        "total_cost": Decimal(str(round(total, 2)))
    }
