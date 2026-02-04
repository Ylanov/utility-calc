import logging
from app.models import User, Tariff

# -------------------------------------------------
# НАСТРОЙКА ЛОГГЕРА
# -------------------------------------------------
# Настраиваем логгер для модуля расчетов. Это позволяет изолировать
# логику логирования и выводить подробную информацию о процессе вычислений,
# что крайне полезно для отладки и проверки корректности начислений.

logger = logging.getLogger("utility_calculations")

# Проверяем, есть ли уже обработчики, чтобы избежать дублирования логов при перезагрузке uvicorn
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | [%(name)s] | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
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

    Выполняет расчет всех компонентов платежа на основе тарифов и потребленных объемов.
    Включает подробное логирование каждого шага для легкой отладки и проверки.
    Содержит защитные механизмы, предотвращающие расчет отрицательной стоимости
    путем приведения отрицательных объемов потребления (например, после коррекций) к нулю.

    Args:
        user: Объект пользователя (User) с данными о площади, жильцах и т.д.
        tariff: Объект тарифов (Tariff).
        volume_hot: Объем потребленной горячей воды (м³).
        volume_cold: Объем потребленной холодной воды (м³).
        volume_sewage: Объем водоотведения (м³).
        volume_electricity_share: Доля потребленной электроэнергии (кВт*ч).

    Returns:
        Словарь (dict) с детализацией стоимости по каждой услуге и итоговой суммой.
    """
    logger.info("=" * 60)
    logger.info(f"НАЧАЛО РАСЧЁТА КОММУНАЛЬНЫХ ПЛАТЕЖЕЙ ДЛЯ ПОЛЬЗОВАТЕЛЯ: {user.username} (ID: {user.id})")
    logger.info(f"Площадь: {user.apartment_area} м² | Жильцов: {user.residents_count}")
    logger.info(
        f"Входные объемы: ГВС={volume_hot}, ХВС={volume_cold}, Канал.={volume_sewage}, Свет={volume_electricity_share}")

    # -------------------------------------------------
    # БЛОК БЕЗОПАСНОСТИ: ЗАЩИТА ОТ ОТРИЦАТЕЛЬНЫХ ЗНАЧЕНИЙ
    # -------------------------------------------------
    # Если из-за ручных коррекций бухгалтера объем потребления стал отрицательным,
    # мы не должны начислять "кредит" пользователю. Стоимость услуги не может быть < 0.
    # Поэтому приводим все отрицательные объемы к нулю перед расчетом.

    safe_volume_hot = max(0, volume_hot)
    safe_volume_cold = max(0, volume_cold)
    safe_volume_sewage = max(0, volume_sewage)
    safe_volume_electricity_share = max(0, volume_electricity_share)

    if volume_hot < 0:
        logger.warning(f"Отрицательный объем ГВС ({volume_hot}) скорректирован до 0.")
    if volume_cold < 0:
        logger.warning(f"Отрицательный объем ХВС ({volume_cold}) скорректирован до 0.")
    if volume_sewage < 0:
        logger.warning(f"Отрицательный объем водоотведения ({volume_sewage}) скорректирован до 0.")
    if volume_electricity_share < 0:
        logger.warning(f"Отрицательный объем электроэнергии ({volume_electricity_share}) скорректирован до 0.")

    # -------------------------------------------------
    # 1. ГОРЯЧАЯ ВОДА (состоит из подачи холодной воды + ее подогрева)
    # -------------------------------------------------
    hot_water_rate = tariff.water_supply + tariff.water_heating
    cost_hot_water = safe_volume_hot * hot_water_rate
    logger.info("-" * 60)
    logger.info("1. Горячая вода")
    logger.info(f"Формула: {safe_volume_hot:.4f} м³ × ({tariff.water_supply} + {tariff.water_heating}) руб/м³")
    logger.info(f"Итоговый тариф на ГВС: {hot_water_rate:.2f} руб/м³")
    logger.info(f"Стоимость: {cost_hot_water:.2f} руб.")

    # -------------------------------------------------
    # 2. ХОЛОДНАЯ ВОДА
    # -------------------------------------------------
    cost_cold_water = safe_volume_cold * tariff.water_supply
    logger.info("-" * 60)
    logger.info("2. Холодная вода")
    logger.info(f"Формула: {safe_volume_cold:.4f} м³ × {tariff.water_supply} руб/м³")
    logger.info(f"Стоимость: {cost_cold_water:.2f} руб.")

    # -------------------------------------------------
    # 3. ВОДООТВЕДЕНИЕ (канализация)
    # -------------------------------------------------
    cost_sewage = safe_volume_sewage * tariff.sewage
    logger.info("-" * 60)
    logger.info("3. Водоотведение")
    logger.info(f"Формула: {safe_volume_sewage:.4f} м³ × {tariff.sewage} руб/м³")
    logger.info(f"Стоимость: {cost_sewage:.2f} руб.")

    # -------------------------------------------------
    # 4. ЭЛЕКТРОЭНЕРГИЯ (индивидуальное потребление)
    # -------------------------------------------------
    cost_electricity = safe_volume_electricity_share * tariff.electricity_rate
    logger.info("-" * 60)
    logger.info("4. Электроэнергия")
    logger.info(f"Формула: {safe_volume_electricity_share:.4f} кВт*ч × {tariff.electricity_rate} руб/кВт*ч")
    logger.info(f"Стоимость: {cost_electricity:.2f} руб.")

    # -------------------------------------------------
    # 5. СОДЕРЖАНИЕ И РЕМОНТ (фиксированная, от площади)
    # -------------------------------------------------
    cost_maintenance = user.apartment_area * tariff.maintenance_repair
    logger.info("-" * 60)
    logger.info("5. Содержание и ремонт")
    logger.info(f"Формула: {user.apartment_area} м² × {tariff.maintenance_repair} руб/м²")
    logger.info(f"Стоимость: {cost_maintenance:.2f} руб.")

    # -------------------------------------------------
    # 6. НАЕМ ЖИЛЬЯ (СОЦ. НАЙМ)
    # -------------------------------------------------
    cost_social_rent = user.apartment_area * tariff.social_rent
    logger.info("-" * 60)
    logger.info("6. Наем жилья (социальный найм)")
    logger.info(f"Формула: {user.apartment_area} м² × {tariff.social_rent} руб/м²")
    logger.info(f"Стоимость: {cost_social_rent:.2f} руб.")

    # -------------------------------------------------
    # 7. ТКО (ВЫВОЗ МУСОРА)
    # -------------------------------------------------
    cost_waste = user.apartment_area * tariff.waste_disposal
    logger.info("-" * 60)
    logger.info("7. ТКО (Вывоз мусора)")
    logger.info(f"Формула: {user.apartment_area} м² × {tariff.waste_disposal} руб/м²")
    logger.info(f"Стоимость: {cost_waste:.2f} руб.")

    # -------------------------------------------------
    # 8. ОСТАЛЬНАЯ ФИКСИРОВАННАЯ ЧАСТЬ (Отопление + ОДН)
    # -------------------------------------------------
    # Здесь суммируются все остальные платежи, зависящие от площади, которые не были выделены ранее.
    fixed_rate_other = tariff.heating + tariff.electricity_per_sqm
    cost_fixed_part = user.apartment_area * fixed_rate_other
    logger.info("-" * 60)
    logger.info("8. Фиксированная часть (Отопление + ОДН Электричество)")
    logger.info(f"Формула: {user.apartment_area} м² × ({tariff.heating} + {tariff.electricity_per_sqm}) руб/м²")
    logger.info(f"Суммарный тариф фикс. части: {fixed_rate_other:.2f} руб/м²")
    logger.info(f"Стоимость: {cost_fixed_part:.2f} руб.")

    # -------------------------------------------------
    # ИТОГОВЫЙ РАСЧЁТ
    # -------------------------------------------------
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
    logger.info(f"КОНЕЦ РАСЧЁТА ДЛЯ ПОЛЬЗОВАТЕЛЯ: {user.username}\n")

    # Возвращаем словарь с округленными до 2 знаков после запятой значениями,
    # что является стандартом для финансовых расчетов.
    return {
        "cost_hot_water": round(cost_hot_water, 2),
        "cost_cold_water": round(cost_cold_water, 2),
        "cost_sewage": round(cost_sewage, 2),
        "cost_electricity": round(cost_electricity, 2),
        "cost_maintenance": round(cost_maintenance, 2),
        "cost_social_rent": round(cost_social_rent, 2),
        "cost_waste": round(cost_waste, 2),
        "cost_fixed_part": round(cost_fixed_part, 2),
        "total_cost": round(total_cost, 2)
    }