import logging
from decimal import Decimal, ROUND_HALF_UP
from app.models import User, Tariff


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
    user: User,
    tariff: Tariff,
    volume_hot: Decimal,
    volume_cold: Decimal,
    volume_sewage: Decimal,
    volume_electricity_share: Decimal
) -> dict:

    vol_hot = safe_positive(D(volume_hot))
    vol_cold = safe_positive(D(volume_cold))
    vol_sewage = safe_positive(D(volume_sewage))
    vol_elect = safe_positive(D(volume_electricity_share))

    area = safe_positive(D(user.apartment_area))

    t_water_supply = D(tariff.water_supply)
    t_water_heating = D(tariff.water_heating)
    t_sewage = D(tariff.sewage)
    t_elect_rate = D(tariff.electricity_rate)
    t_maint = D(tariff.maintenance_repair)
    t_soc_rent = D(tariff.social_rent)
    t_waste = D(tariff.waste_disposal)
    t_heat = D(tariff.heating)
    t_elect_sqm = D(tariff.electricity_per_sqm)

    hot_water_rate = t_water_supply + t_water_heating
    cost_hot_water = quantize_money(vol_hot * hot_water_rate)

    cost_cold_water = quantize_money(vol_cold * t_water_supply)

    cost_sewage = quantize_money(vol_sewage * t_sewage)

    cost_electricity = quantize_money(vol_elect * t_elect_rate)

    cost_maintenance = quantize_money(area * t_maint)

    cost_social_rent = quantize_money(area * t_soc_rent)

    cost_waste = quantize_money(area * t_waste)

    fixed_rate = t_heat + t_elect_sqm
    cost_fixed_part = quantize_money(area * fixed_rate)

    total_cost = (
        cost_hot_water +
        cost_cold_water +
        cost_sewage +
        cost_electricity +
        cost_maintenance +
        cost_social_rent +
        cost_waste +
        cost_fixed_part
    )

    logger.debug(
        "Calculated utilities for user_id=%s total=%s",
        user.id,
        total_cost
    )

    return {
        "cost_hot_water": cost_hot_water,
        "cost_cold_water": cost_cold_water,
        "cost_sewage": cost_sewage,
        "cost_electricity": cost_electricity,
        "cost_maintenance": cost_maintenance,
        "cost_social_rent": cost_social_rent,
        "cost_waste": cost_waste,
        "cost_fixed_part": cost_fixed_part,
        "total_cost": total_cost
    }
