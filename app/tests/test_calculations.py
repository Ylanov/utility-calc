# app/tests/test_calculations.py

from decimal import Decimal
from app.modules.utility.services.calculations import calculate_utilities, quantize_money, safe_positive, D


# ──────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ЗАГЛУШКИ
# ──────────────────────────────────────────────────────────────

class FakeRoom:
    def __init__(self, area=50.0, total_residents=2):
        self.apartment_area = Decimal(str(area))
        self.total_room_residents = total_residents


class FakeUser:
    def __init__(self, residents=2):
        self.residents_count = residents


class FakeTariff:
    def __init__(
        self,
        water_supply="40.00",
        water_heating="150.00",
        sewage="35.00",
        electricity_rate="5.50",
        maintenance_repair="30.50",
        social_rent="5.10",
        waste_disposal="6.50",
        heating="25.00",
        electricity_per_sqm="1.20",
    ):
        self.water_supply      = Decimal(water_supply)
        self.water_heating     = Decimal(water_heating)
        self.sewage            = Decimal(sewage)
        self.electricity_rate  = Decimal(electricity_rate)
        self.maintenance_repair = Decimal(maintenance_repair)
        self.social_rent       = Decimal(social_rent)
        self.waste_disposal    = Decimal(waste_disposal)
        self.heating           = Decimal(heating)
        self.electricity_per_sqm = Decimal(electricity_per_sqm)


# ──────────────────────────────────────────────────────────────
# ТЕСТ 1: Основной расчёт с дробными числами
# ──────────────────────────────────────────────────────────────

def test_calculation_precision():
    """
    Проверяем корректность расчёта с Decimal и ROUND_HALF_UP.
    """
    user   = FakeUser(residents=2)
    room   = FakeRoom(area=45.50, total_residents=2)
    tariff = FakeTariff()

    vol_hot  = Decimal("3.123")
    vol_cold = Decimal("5.789")
    vol_sew  = vol_hot + vol_cold   # 8.912
    vol_el   = Decimal("120.555")

    result = calculate_utilities(
        user=user,
        room=room,
        tariff=tariff,
        volume_hot=vol_hot,
        volume_cold=vol_cold,
        volume_sewage=vol_sew,
        volume_electricity_share=vol_el,
    )

    # ── РУЧНОЙ РАСЧЁТ ──
    # ГВС:  3.123 * (40.00 + 150.00) = 3.123 * 190.00 = 593.37
    assert result["cost_hot_water"] == Decimal("593.37"), f"ГВС: {result['cost_hot_water']}"

    # ХВС:  5.789 * 40.00 = 231.560 → 231.56
    assert result["cost_cold_water"] == Decimal("231.56"), f"ХВС: {result['cost_cold_water']}"

    # Канализация: 8.912 * 35.00 = 311.920 → 311.92
    assert result["cost_sewage"] == Decimal("311.92"), f"Канализация: {result['cost_sewage']}"

    # Электро: 120.555 * 5.50 = 663.0525 → 663.05 (ROUND_HALF_UP)
    assert result["cost_electricity"] == Decimal("663.05"), f"Электро: {result['cost_electricity']}"

    # Содержание: 45.50 * 30.50 = 1387.75
    assert result["cost_maintenance"] == Decimal("1387.75"), f"Содержание: {result['cost_maintenance']}"

    # Наём: 45.50 * 5.10 = 232.05
    assert result["cost_social_rent"] == Decimal("232.05"), f"Наём: {result['cost_social_rent']}"

    # ТКО: 45.50 * 6.50 = 295.75
    assert result["cost_waste"] == Decimal("295.75"), f"ТКО: {result['cost_waste']}"

    # Фиксированная: 45.50 * (25.00 + 1.20) = 45.50 * 26.20 = 1192.10
    assert result["cost_fixed_part"] == Decimal("1192.10"), f"Фикс: {result['cost_fixed_part']}"

    # ИТОГО: 593.37+231.56+311.92+663.05+1387.75+232.05+295.75+1192.10 = 4907.55
    expected = Decimal("4907.55")
    assert result["total_cost"] == expected, f"ИТОГО: {result['total_cost']} != {expected}"

    # total_cost должен совпадать с суммой компонент (нет расхождения копеек)
    components_sum = (
        result["cost_hot_water"] + result["cost_cold_water"] +
        result["cost_sewage"] + result["cost_electricity"] +
        result["cost_maintenance"] + result["cost_social_rent"] +
        result["cost_waste"] + result["cost_fixed_part"]
    )
    assert result["total_cost"] == components_sum, (
        f"total_cost={result['total_cost']} не совпадает с суммой компонент={components_sum}"
    )

    print("✅ test_calculation_precision ПРОЙДЕН")


# ──────────────────────────────────────────────────────────────
# ТЕСТ 2: Защита от отрицательных объёмов
# ──────────────────────────────────────────────────────────────

def test_negative_volumes_give_zero():
    """
    Если объём отрицательный (например счётчик откатили назад),
    результат должен быть 0.00, а не отрицательная сумма.
    """
    user   = FakeUser()
    room   = FakeRoom(area=30.0)
    tariff = FakeTariff()

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("-5.0"),
        volume_cold=Decimal("-1.0"),
        volume_sewage=Decimal("-6.0"),
        volume_electricity_share=Decimal("-10.0"),
    )

    assert result["cost_hot_water"]   == Decimal("0.00"), f"ГВС отриц.: {result['cost_hot_water']}"
    assert result["cost_cold_water"]  == Decimal("0.00"), f"ХВС отриц.: {result['cost_cold_water']}"
    assert result["cost_sewage"]      == Decimal("0.00"), f"Канализ. отриц.: {result['cost_sewage']}"
    assert result["cost_electricity"] == Decimal("0.00"), f"Электро отриц.: {result['cost_electricity']}"

    # Фиксированные начисления должны остаться (они от площади, не от объёма)
    assert result["cost_maintenance"] > Decimal("0.00"), "Содержание должно быть > 0"
    assert result["cost_social_rent"] > Decimal("0.00"), "Наём должен быть > 0"

    print("✅ test_negative_volumes_give_zero ПРОЙДЕН")


# ──────────────────────────────────────────────────────────────
# ТЕСТ 3: ROUND_HALF_UP — граничные случаи округления
# ──────────────────────────────────────────────────────────────

def test_rounding_half_up():
    """
    Проверяем что 0.005 → 0.01, а не 0.00 (банковское).
    Критично для сумм вида X.XXX5.
    """
    # 0.235 * 1 = 0.235 → ROUND_HALF_UP = 0.24 (Python round() = 0.23!)
    room   = FakeRoom(area=1.0)
    user   = FakeUser()
    tariff = FakeTariff(
        water_supply="0.235",
        water_heating="0",
        sewage="0", electricity_rate="0",
        maintenance_repair="0", social_rent="0",
        waste_disposal="0", heating="0", electricity_per_sqm="0"
    )
    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("0"),
        volume_cold=Decimal("1.0"),
        volume_sewage=Decimal("0"),
        volume_electricity_share=Decimal("0"),
    )
    # ХВС = 1.0 * 0.235 = 0.235 → ROUND_HALF_UP → 0.24
    assert result["cost_cold_water"] == Decimal("0.24"), (
        f"Округление 0.235: ожидается 0.24, получено {result['cost_cold_water']}"
    )
    print("✅ test_rounding_half_up ПРОЙДЕН")


# ──────────────────────────────────────────────────────────────
# ТЕСТ 4: Нулевые объёмы — только фиксированные начисления
# ──────────────────────────────────────────────────────────────

def test_zero_consumption():
    """
    Если жилец не подавал воду/свет (нулевой расход),
    счётчиковые части = 0, фиксированные части остаются.
    """
    user   = FakeUser()
    room   = FakeRoom(area=20.0)
    tariff = FakeTariff(
        water_supply="40.00", water_heating="150.00",
        sewage="35.00", electricity_rate="5.50",
        maintenance_repair="30.50", social_rent="5.10",
        waste_disposal="6.50", heating="25.00", electricity_per_sqm="1.20",
    )

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("0"),
        volume_cold=Decimal("0"),
        volume_sewage=Decimal("0"),
        volume_electricity_share=Decimal("0"),
    )

    assert result["cost_hot_water"]   == Decimal("0.00")
    assert result["cost_cold_water"]  == Decimal("0.00")
    assert result["cost_sewage"]      == Decimal("0.00")
    assert result["cost_electricity"] == Decimal("0.00")

    # Содержание: 20.0 * 30.50 = 610.00
    assert result["cost_maintenance"] == Decimal("610.00")
    # Наём: 20.0 * 5.10 = 102.00
    assert result["cost_social_rent"] == Decimal("102.00")
    # ТКО: 20.0 * 6.50 = 130.00
    assert result["cost_waste"]       == Decimal("130.00")
    # Фикс: 20.0 * (25.00 + 1.20) = 20.0 * 26.20 = 524.00
    assert result["cost_fixed_part"]  == Decimal("524.00")

    expected_total = Decimal("1366.00")
    assert result["total_cost"] == expected_total, f"ИТОГО: {result['total_cost']}"

    print("✅ test_zero_consumption ПРОЙДЕН")


# ──────────────────────────────────────────────────────────────
# ТЕСТ 5: Частичный месяц (выселение) — fraction < 1
# ──────────────────────────────────────────────────────────────

def test_fraction_partial_month():
    """
    При выселении в середине месяца фиксированные платежи
    начисляются пропорционально прожитым дням.
    """
    user   = FakeUser()
    room   = FakeRoom(area=30.0)
    tariff = FakeTariff(
        water_supply="0", water_heating="0",
        sewage="0", electricity_rate="0",
        maintenance_repair="100.00", social_rent="0",
        waste_disposal="0", heating="0", electricity_per_sqm="0",
    )
    # Прожил 15 из 30 дней → fraction = 0.5
    fraction = Decimal("15") / Decimal("30")

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("0"),
        volume_cold=Decimal("0"),
        volume_sewage=Decimal("0"),
        volume_electricity_share=Decimal("0"),
        fraction=fraction,
    )

    # Содержание: 30.0 * 100.00 * 0.5 = 1500.00
    assert result["cost_maintenance"] == Decimal("1500.00"), (
        f"Fraction содержание: {result['cost_maintenance']}"
    )

    print("✅ test_fraction_partial_month ПРОЙДЕН")


# ──────────────────────────────────────────────────────────────
# ТЕСТ 6: total_cost = сумма компонент (нет расхождения)
# ──────────────────────────────────────────────────────────────

def test_total_equals_sum_of_components():
    """
    Гарантируем что total_cost == сумма всех составляющих.
    Нарушение этого свойства означает накопление погрешности.
    """
    user   = FakeUser()
    room   = FakeRoom(area=47.33)  # нестандартная площадь
    tariff = FakeTariff(
        water_supply="42.17",
        water_heating="163.55",
        sewage="37.82",
        electricity_rate="5.74",
        maintenance_repair="31.23",
        social_rent="5.47",
        waste_disposal="7.13",
        heating="26.44",
        electricity_per_sqm="1.35",
    )

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("4.712"),
        volume_cold=Decimal("7.339"),
        volume_sewage=Decimal("12.051"),
        volume_electricity_share=Decimal("87.663"),
    )

    components_sum = (
        result["cost_hot_water"] + result["cost_cold_water"] +
        result["cost_sewage"]    + result["cost_electricity"] +
        result["cost_maintenance"] + result["cost_social_rent"] +
        result["cost_waste"]     + result["cost_fixed_part"]
    )

    assert result["total_cost"] == components_sum, (
        f"total_cost={result['total_cost']} != сумма компонент={components_sum}"
    )

    print("✅ test_total_equals_sum_of_components ПРОЙДЕН")


# ──────────────────────────────────────────────────────────────
# ТЕСТ 7: Вспомогательные функции
# ──────────────────────────────────────────────────────────────

def test_helper_functions():
    """Проверяем D(), quantize_money(), safe_positive()."""

    # D()
    assert D(None)           == Decimal("0.00")
    assert D(5)              == Decimal("5")
    assert D(3.14)           == Decimal("3.14")
    assert D("2.718")        == Decimal("2.718")
    assert D(Decimal("1.5")) == Decimal("1.5")

    # quantize_money() — ROUND_HALF_UP
    assert quantize_money(Decimal("0.005"))  == Decimal("0.01")  # не 0.00!
    assert quantize_money(Decimal("0.235"))  == Decimal("0.24")  # не 0.23!
    assert quantize_money(Decimal("0.245"))  == Decimal("0.25")  # не 0.24!
    assert quantize_money(Decimal("10.004")) == Decimal("10.00")
    assert quantize_money(Decimal("10.005")) == Decimal("10.01")

    # safe_positive()
    assert safe_positive(Decimal("5.0"))  == Decimal("5.0")
    assert safe_positive(Decimal("0.0"))  == Decimal("0.0")
    assert safe_positive(Decimal("-3.0")) == Decimal("0.00")

    print("✅ test_helper_functions ПРОЙДЕН")


# ──────────────────────────────────────────────────────────────
# ЗАПУСК ВСЕХ ТЕСТОВ
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_helper_functions()
    test_calculation_precision()
    test_negative_volumes_give_zero()
    test_rounding_half_up()
    test_zero_consumption()
    test_fraction_partial_month()
    test_total_equals_sum_of_components()
    print("\n🎉 Все тесты пройдены успешно!")