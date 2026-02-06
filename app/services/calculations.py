import logging
from decimal import Decimal, ROUND_HALF_UP
from app.models import User, Tariff

# -------------------------------------------------
# НАСТРОЙКА ЛОГГЕРА
# -------------------------------------------------
logger = logging.getLogger("utility_calculations")

if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | [%(name)s] | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )


# -------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (DECIMAL)
# -------------------------------------------------

def D(value) -> Decimal:
    """
    Безопасное преобразование входных данных в Decimal.
    Обрабатывает float через строковое представление, чтобы избежать
    артефактов плавающей точки (например, 1.1 -> 1.10000000000000008).
    """
    if value is None:
        return Decimal("0.00")
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(value)


def quantize_money(value: Decimal) -> Decimal:
    """
    Округление денежных сумм до 2 знаков после запятой (до копеек).
    Использует математическое округление (ROUND_HALF_UP).
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# -------------------------------------------------
# ОСНОВНАЯ ФУНКЦИЯ РАСЧЁТА
# -------------------------------------------------

def calculate_utilities(
        user: User,
        tariff: Tariff,
        volume_hot: Decimal,
        volume_cold: Decimal,
        volume_sewage: Decimal,
        volume_electricity_share: Decimal
) -> dict:
    """
    Универсальная функция расчета стоимости коммунальных услуг с использованием Decimal.

    Выполняет расчет всех компонентов платежа.
    Каждая позиция округляется до копеек ПЕРЕД суммированием,
    чтобы сумма строк в квитанции совпадала с итогом.
    """

    # 1. Приводим все входные данные к Decimal
    # Даже если модели SQLAlchemy уже возвращают Decimal, это защитный слой
    vol_hot = D(volume_hot)
    vol_cold = D(volume_cold)
    vol_sewage = D(volume_sewage)
    vol_elect_share = D(volume_electricity_share)

    area = D(user.apartment_area)

    # Тарифы
    t_water_supply = D(tariff.water_supply)
    t_water_heating = D(tariff.water_heating)
    t_sewage = D(tariff.sewage)
    t_elect_rate = D(tariff.electricity_rate)
    t_maint = D(tariff.maintenance_repair)
    t_soc_rent = D(tariff.social_rent)
    t_waste = D(tariff.waste_disposal)
    t_heat = D(tariff.heating)
    t_elect_sqm = D(tariff.electricity_per_sqm)

    logger.info("=" * 60)
    logger.info(f"НАЧАЛО РАСЧЁТА (DECIMAL): {user.username} (ID: {user.id})")
    logger.info(f"Площадь: {area} м² | Жильцов: {user.residents_count}")
    logger.info(f"Входные объемы: ГВС={vol_hot}, ХВС={vol_cold}, Канал.={vol_sewage}, Свет={vol_elect_share}")

    # -------------------------------------------------
    # БЛОК БЕЗОПАСНОСТИ: ЗАЩИТА ОТ ОТРИЦАТЕЛЬНЫХ ЗНАЧЕНИЙ
    # -------------------------------------------------
    safe_vol_hot = max(Decimal("0"), vol_hot)
    safe_vol_cold = max(Decimal("0"), vol_cold)
    safe_vol_sewage = max(Decimal("0"), vol_sewage)
    safe_vol_elect = max(Decimal("0"), vol_elect_share)

    if vol_hot < 0:
        logger.warning(f"Отрицательный объем ГВС ({vol_hot}) скорректирован до 0.")
    if vol_cold < 0:
        logger.warning(f"Отрицательный объем ХВС ({vol_cold}) скорректирован до 0.")

    # -------------------------------------------------
    # 1. ГОРЯЧАЯ ВОДА
    # -------------------------------------------------
    # Тариф на ГВС = Подача воды + Нагрев
    hot_water_rate = t_water_supply + t_water_heating
    raw_cost_hot = safe_vol_hot * hot_water_rate
    cost_hot_water = quantize_money(raw_cost_hot)

    logger.info("-" * 60)
    logger.info("1. Горячая вода")
    logger.info(f"Формула: {safe_vol_hot} м³ × ({t_water_supply} + {t_water_heating})")
    logger.info(f"Стоимость: {cost_hot_water} руб.")

    # -------------------------------------------------
    # 2. ХОЛОДНАЯ ВОДА
    # -------------------------------------------------
    raw_cost_cold = safe_vol_cold * t_water_supply
    cost_cold_water = quantize_money(raw_cost_cold)

    logger.info("-" * 60)
    logger.info("2. Холодная вода")
    logger.info(f"Формула: {safe_vol_cold} м³ × {t_water_supply}")
    logger.info(f"Стоимость: {cost_cold_water} руб.")

    # -------------------------------------------------
    # 3. ВОДООТВЕДЕНИЕ
    # -------------------------------------------------
    raw_cost_sewage = safe_vol_sewage * t_sewage
    cost_sewage = quantize_money(raw_cost_sewage)

    logger.info("-" * 60)
    logger.info("3. Водоотведение")
    logger.info(f"Формула: {safe_vol_sewage} м³ × {t_sewage}")
    logger.info(f"Стоимость: {cost_sewage} руб.")

    # -------------------------------------------------
    # 4. ЭЛЕКТРОЭНЕРГИЯ
    # -------------------------------------------------
    raw_cost_elect = safe_vol_elect * t_elect_rate
    cost_electricity = quantize_money(raw_cost_elect)

    logger.info("-" * 60)
    logger.info("4. Электроэнергия")
    logger.info(f"Формула: {safe_vol_elect} кВт*ч × {t_elect_rate}")
    logger.info(f"Стоимость: {cost_electricity} руб.")

    # -------------------------------------------------
    # 5. СОДЕРЖАНИЕ И РЕМОНТ
    # -------------------------------------------------
    cost_maintenance = quantize_money(area * t_maint)

    logger.info("-" * 60)
    logger.info("5. Содержание и ремонт")
    logger.info(f"Формула: {area} м² × {t_maint}")
    logger.info(f"Стоимость: {cost_maintenance} руб.")

    # -------------------------------------------------
    # 6. НАЕМ ЖИЛЬЯ
    # -------------------------------------------------
    cost_social_rent = quantize_money(area * t_soc_rent)

    logger.info("-" * 60)
    logger.info("6. Наем жилья")
    logger.info(f"Формула: {area} м² × {t_soc_rent}")
    logger.info(f"Стоимость: {cost_social_rent} руб.")

    # -------------------------------------------------
    # 7. ТКО (ВЫВОЗ МУСОРА)
    # -------------------------------------------------
    cost_waste = quantize_money(area * t_waste)

    logger.info("-" * 60)
    logger.info("7. ТКО")
    logger.info(f"Формула: {area} м² × {t_waste}")
    logger.info(f"Стоимость: {cost_waste} руб.")

    # -------------------------------------------------
    # 8. ФИКСИРОВАННАЯ ЧАСТЬ (Отопление + ОДН)
    # -------------------------------------------------
    fixed_rate = t_heat + t_elect_sqm
    cost_fixed_part = quantize_money(area * fixed_rate)

    logger.info("-" * 60)
    logger.info("8. Фиксированная часть (Отопление + ОДН)")
    logger.info(f"Формула: {area} м² × ({t_heat} + {t_elect_sqm})")
    logger.info(f"Стоимость: {cost_fixed_part} руб.")

    # -------------------------------------------------
    # ИТОГОВЫЙ РАСЧЁТ
    # -------------------------------------------------
    # Суммируем уже округленные значения
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

    logger.info("=" * 60)
    logger.info("ИТОГОВЫЙ РАСЧЁТ")
    logger.info(f"  Горячая вода:       {cost_hot_water:10.2f} руб.")
    logger.info(f"  Холодная вода:      {cost_cold_water:10.2f} руб.")
    logger.info(f"  Водоотведение:      {cost_sewage:10.2f} руб.")
    logger.info(f"  Электроэнергия:     {cost_electricity:10.2f} руб.")
    logger.info(f"  Содержание:         {cost_maintenance:10.2f} руб.")
    logger.info(f"  Наем жилья:         {cost_social_rent:10.2f} руб.")
    logger.info(f"  Вывоз мусора (ТКО): {cost_waste:10.2f} руб.")
    logger.info(f"  Отопление + ОДН:    {cost_fixed_part:10.2f} руб.")
    logger.info("-" * 40)
    logger.info(f"  ИТОГО К ОПЛАТЕ:     {total_cost:10.2f} руб.")
    logger.info("=" * 60)

    # Возвращаем Decimal объекты. Pydantic в схемах сам преобразует их
    # в JSON-совместимый формат (строку или float, в зависимости от настроек),
    # но внутри Python мы сохраняем точность.
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