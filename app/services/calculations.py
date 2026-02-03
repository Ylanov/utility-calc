import logging

from app.models import User, Tariff


# -------------------------------------------------
# НАСТРОЙКА ЛОГГЕРА
# -------------------------------------------------

logger = logging.getLogger("utility_calculations")

if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )


# -------------------------------------------------
# ОСНОВНАЯ ФУНКЦИЯ РАСЧЁТА
# -------------------------------------------------

def calculate_utilities(
    user: User,
    tariff: Tariff,
    volume_hot: float,
    volume_cold: float,
    volume_sewage: float,
    volume_electricity_share: float
) -> dict:
    """
    Универсальная функция расчета стоимости коммунальных услуг.
    С ПОДРОБНЫМ ЛОГИРОВАНИЕМ ВСЕХ ВЫЧИСЛЕНИЙ.
    """

    logger.info("=" * 60)
    logger.info("НАЧАЛО РАСЧЁТА КОММУНАЛЬНЫХ ПЛАТЕЖЕЙ")

    # -------------------------------------------------
    # ВХОДНЫЕ ДАННЫЕ
    # -------------------------------------------------

    logger.info("Входные данные:")

    logger.info(f"Пользователь: {user.username}")
    logger.info(f"Площадь квартиры: {user.apartment_area} м²")
    logger.info(f"Количество жильцов: {user.residents_count}")

    logger.info(f"Объем горячей воды: {volume_hot}")
    logger.info(f"Объем холодной воды: {volume_cold}")
    logger.info(f"Объем водоотведения: {volume_sewage}")
    logger.info(f"Доля электроэнергии: {volume_electricity_share}")

    logger.info("Тарифы:")

    logger.info(f"Подогрев воды: {tariff.water_heating}")
    logger.info(f"Подача воды: {tariff.water_supply}")
    logger.info(f"Водоотведение: {tariff.sewage}")
    logger.info(f"Электроэнергия: {tariff.electricity_rate}")
    logger.info(f"Содержание/ремонт: {tariff.maintenance_repair}")
    logger.info(f"Соц.найм: {tariff.social_rent}")
    logger.info(f"Отопление: {tariff.heating}")
    logger.info(f"Электричество (м²): {tariff.electricity_per_sqm}")
    logger.info(f"Вывоз мусора: {tariff.waste_disposal}")

    # -------------------------------------------------
    # 1. ГОРЯЧАЯ ВОДА
    # -------------------------------------------------

    hot_rate = tariff.water_heating + tariff.water_supply

    cost_hot = volume_hot * hot_rate

    logger.info("-" * 60)
    logger.info("1. Горячая вода")
    logger.info(f"Формула: {volume_hot} × ({tariff.water_heating} + {tariff.water_supply})")
    logger.info(f"Тариф: {hot_rate}")
    logger.info(f"Стоимость: {cost_hot:.2f}")

    # -------------------------------------------------
    # 2. ХОЛОДНАЯ ВОДА
    # -------------------------------------------------

    cost_cold = volume_cold * tariff.water_supply

    logger.info("-" * 60)
    logger.info("2. Холодная вода")
    logger.info(f"Формула: {volume_cold} × {tariff.water_supply}")
    logger.info(f"Стоимость: {cost_cold:.2f}")

    # -------------------------------------------------
    # 3. ВОДООТВЕДЕНИЕ
    # -------------------------------------------------

    cost_sewage = volume_sewage * tariff.sewage

    logger.info("-" * 60)
    logger.info("3. Водоотведение")
    logger.info(f"Формула: {volume_sewage} × {tariff.sewage}")
    logger.info(f"Стоимость: {cost_sewage:.2f}")

    # -------------------------------------------------
    # 4. ЭЛЕКТРИЧЕСТВО
    # -------------------------------------------------

    cost_elect = volume_electricity_share * tariff.electricity_rate

    logger.info("-" * 60)
    logger.info("4. Электроэнергия")
    logger.info(f"Формула: {volume_electricity_share} × {tariff.electricity_rate}")
    logger.info(f"Стоимость: {cost_elect:.2f}")

    # -------------------------------------------------
    # 5. СОДЕРЖАНИЕ И РЕМОНТ
    # -------------------------------------------------

    cost_maintenance = user.apartment_area * tariff.maintenance_repair

    logger.info("-" * 60)
    logger.info("5. Содержание и ремонт")
    logger.info(f"Формула: {user.apartment_area} × {tariff.maintenance_repair}")
    logger.info(f"Стоимость: {cost_maintenance:.2f}")

    # -------------------------------------------------
    # 6. НАЕМ ЖИЛЬЯ (СОЦ. НАЙМ)
    # -------------------------------------------------

    cost_social_rent = user.apartment_area * tariff.social_rent

    logger.info("-" * 60)
    logger.info("6. Наем жилья (Соц. найм)")
    logger.info(f"Формула: {user.apartment_area} (м²) × {tariff.social_rent}")
    logger.info(f"Стоимость: {cost_social_rent:.2f}")

    # -------------------------------------------------
    # 7. ТКО (ВЫВОЗ МУСОРА)
    # -------------------------------------------------

    cost_waste = user.apartment_area * tariff.waste_disposal

    logger.info("-" * 60)
    logger.info("7. ТКО (Вывоз мусора)")
    logger.info(f"Формула: {user.apartment_area} (м²) × {tariff.waste_disposal}")
    logger.info(f"Стоимость: {cost_waste:.2f}")

    # -------------------------------------------------
    # 8. ОСТАЛЬНАЯ ФИКСИРОВАННАЯ ЧАСТЬ (Отопление + ОДН)
    # -------------------------------------------------

    # Здесь только Отопление и Электричество (ОДН)
    # Наем и Мусор уже посчитаны выше
    fixed_rate_other = (
        tariff.heating +
        tariff.electricity_per_sqm
    )

    cost_fixed_part = user.apartment_area * fixed_rate_other

    logger.info("-" * 60)
    logger.info("8. Фиксированная часть (Отопление + ОДН)")
    logger.info(f"Формула: {user.apartment_area} × ({tariff.heating} + {tariff.electricity_per_sqm})")
    logger.info(f"Тариф (сумма): {fixed_rate_other}")
    logger.info(f"Стоимость: {cost_fixed_part:.2f}")

    # -------------------------------------------------
    # ИТОГ
    # -------------------------------------------------

    total_cost = (
        cost_hot +
        cost_cold +
        cost_sewage +
        cost_elect +
        cost_maintenance +
        cost_social_rent +  # Наем
        cost_waste +        # Мусор
        cost_fixed_part     # Отопление + ОДН
    )

    logger.info("=" * 60)
    logger.info("ИТОГОВЫЙ РАСЧЁТ")

    logger.info(f"Горячая вода: {cost_hot:.2f}")
    logger.info(f"Холодная вода: {cost_cold:.2f}")
    logger.info(f"Водоотведение: {cost_sewage:.2f}")
    logger.info(f"Электроэнергия: {cost_elect:.2f}")
    logger.info(f"Содержание: {cost_maintenance:.2f}")
    logger.info(f"Наем жилья: {cost_social_rent:.2f}")
    logger.info(f"Вывоз мусора: {cost_waste:.2f}")
    logger.info(f"Отопление + ОДН: {cost_fixed_part:.2f}")

    logger.info(f"ИТОГО К ОПЛАТЕ: {total_cost:.2f}")

    logger.info("=" * 60)
    logger.info("КОНЕЦ РАСЧЁТА\n")

    return {
        "cost_hot_water": round(cost_hot, 2),
        "cost_cold_water": round(cost_cold, 2),
        "cost_sewage": round(cost_sewage, 2),
        "cost_electricity": round(cost_elect, 2),
        "cost_maintenance": round(cost_maintenance, 2),
        "cost_social_rent": round(cost_social_rent, 2),
        "cost_waste": round(cost_waste, 2),
        "cost_fixed_part": round(cost_fixed_part, 2),
        "total_cost": round(total_cost, 2)
    }