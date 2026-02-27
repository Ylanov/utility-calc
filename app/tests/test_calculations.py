from decimal import Decimal
from app.modules.utility.models import User, Tariff
from app.modules.utility.services.calculations import calculate_utilities


# Хелпер для создания заглушек
def create_dummy_user(area=50.0, residents=2):
    return User(
        username="test_user",
        apartment_area=Decimal(str(area)),
        residents_count=residents,
        total_room_residents=residents
    )


def create_dummy_tariff():
    return Tariff(
        maintenance_repair=Decimal("30.50"),  # Содержание
        social_rent=Decimal("5.10"),  # Наем
        heating=Decimal("25.00"),  # Отопление
        water_heating=Decimal("150.00"),  # Нагрев воды (ГВС)
        water_supply=Decimal("40.00"),  # Подача воды (ХВС)
        sewage=Decimal("35.00"),  # Водоотведение
        waste_disposal=Decimal("6.50"),  # Мусор
        electricity_per_sqm=Decimal("1.20"),  # ОДН свет
        electricity_rate=Decimal("5.50")  # Свет тариф
    )


def test_calculation_precision():
    """
    Проверяем сложный кейс с дробными числами, чтобы убедиться в работе Decimal.
    """
    user = create_dummy_user(area=45.50)  # 45.5 кв.м
    tariff = create_dummy_tariff()

    # Входные данные (объемы) с 3 знаками
    vol_hot = Decimal("3.123")
    vol_cold = Decimal("5.789")
    vol_elect = Decimal("120.555")

    # Водоотведение = сумма воды
    vol_sewage = vol_hot + vol_cold  # 8.912

    result = calculate_utilities(
        user=user,
        tariff=tariff,
        volume_hot=vol_hot,
        volume_cold=vol_cold,
        volume_sewage=vol_sewage,
        volume_electricity_share=vol_elect
    )

    # --- РУЧНОЙ РАСЧЕТ ОЖИДАЕМЫХ ЗНАЧЕНИЙ ---

    # 1. ГВС: 3.123 * (40.00 + 150.00) = 3.123 * 190.00 = 593.37
    assert result["cost_hot_water"] == Decimal("593.37")

    # 2. ХВС: 5.789 * 40.00 = 231.56 (231.560 -> 231.56)
    assert result["cost_cold_water"] == Decimal("231.56")

    # 3. Канализация: 8.912 * 35.00 = 311.92 (311.920 -> 311.92)
    assert result["cost_sewage"] == Decimal("311.92")

    # 4. Свет: 120.555 * 5.50 = 663.0525 -> округляем до 663.05
    assert result["cost_electricity"] == Decimal("663.05")

    # 5. Содержание: 45.50 * 30.50 = 1387.75
    assert result["cost_maintenance"] == Decimal("1387.75")

    # 6. Наем: 45.50 * 5.10 = 232.05
    assert result["cost_social_rent"] == Decimal("232.05")

    # 7. Мусор: 45.50 * 6.50 = 295.75
    assert result["cost_waste"] == Decimal("295.75")

    # 8. Фикс (Отопление + ОДН): 45.50 * (25.00 + 1.20) = 45.50 * 26.20 = 1192.10
    assert result["cost_fixed_part"] == Decimal("1192.10")

    # ИТОГО СУММА:
    # 593.37 + 231.56 + 311.92 + 663.05 + 1387.75 + 232.05 + 295.75 + 1192.10
    expected_total = Decimal("4907.55")

    assert result["total_cost"] == expected_total

    print("\n✅ Тест калькулятора (Decimal) пройден успешно!")


def test_negative_values_protection():
    """Проверяем, что отрицательные объемы считаются как 0"""
    user = create_dummy_user()
    tariff = create_dummy_tariff()

    result = calculate_utilities(
        user=user,
        tariff=tariff,
        volume_hot=Decimal("-5.0"),
        volume_cold=Decimal("-1.0"),
        volume_sewage=Decimal("-6.0"),
        volume_electricity_share=Decimal("-10.0")
    )

    assert result["cost_hot_water"] == Decimal("0.00")
    assert result["cost_electricity"] == Decimal("0.00")