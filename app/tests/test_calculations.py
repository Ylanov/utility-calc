# app/tests/test_calculations.py

import pytest
from decimal import Decimal
from app.modules.utility.services.calculations import (
    calculate_utilities,
    calculate_per_capita,
    costs_for_model_fields,
    quantize_money,
    safe_positive,
    D,
    CalculationError,
)


# ──────────────────────────────────────────────────────────────
# ВСПОМОГАТЕЛЬНЫЕ ЗАГЛУШКИ
# ──────────────────────────────────────────────────────────────

class FakeRoom:
    def __init__(self, area=50.0, total_residents=2, is_singles_apartment=False,
                 max_capacity=None):
        self.apartment_area = Decimal(str(area))
        self.total_room_residents = total_residents
        # Bug AS: пометка холостяцкой квартиры (счётчики делятся на факт.
        # число жильцов; area-based статьи — по max_capacity).
        self.is_singles_apartment = is_singles_apartment
        # Макс. вместимость — делитель площади для area-based статей
        # холостяков (area / max_capacity). None → fallback на факт. жильцов.
        self.max_capacity = max_capacity


class FakeUser:
    def __init__(self, residents=2, has_hw_meter=True, has_cw_meter=True, has_el_meter=True):
        self.residents_count = residents
        # Конфигурация счётчиков (см. meters_001_per_user_config). По умолчанию
        # все три True — старое поведение (есть все счётчики).
        self.has_hw_meter = has_hw_meter
        self.has_cw_meter = has_cw_meter
        self.has_el_meter = has_el_meter


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
        electricity_per_sqm="0.00",  # DEPRECATED: ОДН удалён 29.05.2026, в расчёте не участвует
        hw_norm_per_capita="0.000",
        cw_norm_per_capita="0.000",
        el_norm_per_capita="0.000",
        singles_skip_maintenance=False,
        singles_skip_social_rent=False,
        singles_skip_heating=False,
        singles_skip_waste=False,
        # Bug AT: глобальные charge-флаги, default True (zero-impact).
        charge_hot_water=True,
        charge_cold_water=True,
        charge_sewage=True,
        charge_electricity=True,
        charge_maintenance=True,
        charge_social_rent=True,
        charge_heating=True,
        charge_waste=True,
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
        # Нормативы для жильцов без счётчика (meters_001_per_user_config)
        self.hw_norm_per_capita = Decimal(hw_norm_per_capita)
        self.cw_norm_per_capita = Decimal(cw_norm_per_capita)
        self.el_norm_per_capita = Decimal(el_norm_per_capita)
        # Bug AS: skip-флаги для холостяцких квартир.
        self.singles_skip_maintenance = singles_skip_maintenance
        self.singles_skip_social_rent = singles_skip_social_rent
        self.singles_skip_heating = singles_skip_heating
        self.singles_skip_waste = singles_skip_waste
        # Bug AT: «что начисляет тариф».
        self.charge_hot_water = charge_hot_water
        self.charge_cold_water = charge_cold_water
        self.charge_sewage = charge_sewage
        self.charge_electricity = charge_electricity
        self.charge_maintenance = charge_maintenance
        self.charge_social_rent = charge_social_rent
        self.charge_heating = charge_heating
        self.charge_waste = charge_waste


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
    # ГВС (Bug AP/AQ): 3.123 * 150.00 = 468.45 (только water_heating,
    # без сложения с water_supply — тариф уже включает воду).
    assert result["cost_hot_water"] == Decimal("468.45"), f"ГВС: {result['cost_hot_water']}"

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

    # Фиксированная (только отопление, ОДН удалён): 45.50 * 25.00 = 1137.50
    assert result["cost_fixed_part"] == Decimal("1137.50"), f"Фикс: {result['cost_fixed_part']}"

    # ИТОГО: 468.45+231.56+311.92+663.05+1387.75+232.05+295.75+1137.50 = 4728.03
    expected = Decimal("4728.03")
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
        waste_disposal="6.50", heating="25.00",
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
    # Фикс (только отопление, ОДН удалён): 20.0 * 25.00 = 500.00
    assert result["cost_fixed_part"]  == Decimal("500.00")

    expected_total = Decimal("1342.00")
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
# ТЕСТ 8: FAIL-LOUD при пустом тарифе
# ──────────────────────────────────────────────────────────────

def test_calculate_raises_when_all_rates_zero():
    """Если ВСЕ тарифные поля = 0 — это либо неактивный тариф, либо
    некорректно созданный. Раньше функция тихо возвращала 0 (жилец
    видел «зеленую квитанцию», бухгалтерия удивлялась через месяц).
    Теперь — явная ошибка.
    """
    user = FakeUser()
    room = FakeRoom(area=30.0)
    tariff = FakeTariff(
        water_supply="0", water_heating="0", sewage="0",
        electricity_rate="0", maintenance_repair="0", social_rent="0",
        waste_disposal="0", heating="0", electricity_per_sqm="0",
    )
    with pytest.raises(CalculationError, match="Тариф полностью пустой"):
        calculate_utilities(
            user=user, room=room, tariff=tariff,
            volume_hot=Decimal("3"), volume_cold=Decimal("5"),
            volume_sewage=Decimal("8"), volume_electricity_share=Decimal("100"),
        )


def test_calculate_passes_when_one_rate_nonzero():
    """Если хотя бы одно поле ≠ 0 — это валидный (хоть и редкий) тариф,
    например «только содержание». Не raise.
    """
    user = FakeUser()
    room = FakeRoom(area=30.0)
    tariff = FakeTariff(
        water_supply="0", water_heating="0", sewage="0",
        electricity_rate="0",
        maintenance_repair="50.00",  # одно ненулевое поле
        social_rent="0", waste_disposal="0", heating="0", electricity_per_sqm="0",
    )
    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("0"), volume_cold=Decimal("0"),
        volume_sewage=Decimal("0"), volume_electricity_share=Decimal("0"),
    )
    assert result["total_cost"] == Decimal("1500.00")  # 30 * 50
    assert result["sanity_warning"] is None


# ──────────────────────────────────────────────────────────────
# ТЕСТ 9: sanity_warning при подозрительно большом счёте
# ──────────────────────────────────────────────────────────────

def test_sanity_warning_for_huge_total():
    """При итоге > 100k руб должен прийти sanity_warning. НЕ raise —
    редкие легитимные случаи возможны (большие семьи, накопленный долг).
    """
    user = FakeUser()
    room = FakeRoom(area=100.0)
    tariff = FakeTariff(
        water_supply="500.00", water_heating="500.00",  # катастрофичные тарифы
    )
    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("100"), volume_cold=Decimal("100"),
        volume_sewage=Decimal("200"), volume_electricity_share=Decimal("500"),
    )
    assert result["total_cost"] > Decimal("100000")
    assert result["sanity_warning"] is not None
    assert "необычно высока" in result["sanity_warning"]


def test_no_sanity_warning_for_normal_total():
    """Типичный месячный счёт 3-15k — никаких warnings."""
    user = FakeUser()
    room = FakeRoom(area=45.50)
    tariff = FakeTariff()
    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3"), volume_cold=Decimal("5"),
        volume_sewage=Decimal("8"), volume_electricity_share=Decimal("100"),
    )
    assert result["total_cost"] < Decimal("10000")
    assert result["sanity_warning"] is None


# ──────────────────────────────────────────────────────────────
# ТЕСТ 10: per_capita совместим с calculate_utilities (ключи)
# ──────────────────────────────────────────────────────────────

def test_per_capita_has_same_keys():
    """Caller (admin/mobile) не должен различать ветку — оба возвращают
    одинаковый dict с теми же ключами. Иначе сломается общий setattr-loop.
    """
    user = FakeUser()
    user.billing_mode = "per_capita"
    room = FakeRoom(area=30.0)
    tariff = FakeTariff()
    tariff.per_capita_amount = Decimal("3500.00")

    pc_keys = set(calculate_per_capita(user, tariff).keys())
    util_keys = set(
        calculate_utilities(
            user=FakeUser(), room=room, tariff=FakeTariff(),
            volume_hot=Decimal("1"), volume_cold=Decimal("1"),
            volume_sewage=Decimal("2"), volume_electricity_share=Decimal("10"),
        ).keys()
    )
    assert pc_keys == util_keys, (
        f"per_capita keys: {pc_keys}, calculate_utilities keys: {util_keys}"
    )


def test_per_capita_no_longer_routed():
    """2026-06-19: legacy per_capita-шорткат УБРАН. billing_mode='per_capita'
    больше НЕ обнуляет/не фиксирует счёт — calculate_utilities считает по
    счётчикам как обычно (расход × тариф + area-based), а НЕ per_capita_amount."""
    user = FakeUser()
    user.billing_mode = "per_capita"
    room = FakeRoom(area=30.0)
    tariff = FakeTariff()
    tariff.per_capita_amount = Decimal("3500.00")

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("10"), volume_cold=Decimal("10"),
        volume_sewage=Decimal("20"), volume_electricity_share=Decimal("50"),
    )
    # НЕ фиксированные 3500 — считается по счётчикам/площади.
    assert result["total_cost"] != Decimal("3500.00")
    assert result["cost_hot_water"] > Decimal("0")  # расход начислен, не обнулён


# ──────────────────────────────────────────────────────────────
# ТЕСТ 11: costs_for_model_fields фильтрует сервисные ключи
# ──────────────────────────────────────────────────────────────

def test_costs_for_model_fields_filters_meta():
    """sanity_warning и total_cost — НЕ поля модели MeterReading,
    helper их обязан выкидывать. Иначе **costs упадёт с TypeError
    на лишнем kwargs."""
    fake_calc_result = {
        "cost_hot_water": Decimal("100"),
        "cost_cold_water": Decimal("200"),
        "cost_sewage": Decimal("50"),
        "cost_electricity": Decimal("300"),
        "cost_maintenance": Decimal("400"),
        "cost_social_rent": Decimal("80"),
        "cost_waste": Decimal("20"),
        "cost_fixed_part": Decimal("150"),
        "total_cost": Decimal("1300"),
        "sanity_warning": "что-то подозрительное",
    }
    safe = costs_for_model_fields(fake_calc_result)
    assert "total_cost" not in safe
    assert "sanity_warning" not in safe
    assert all(k.startswith("cost_") for k in safe.keys())
    assert len(safe) == 8


# ──────────────────────────────────────────────────────────────
# ТЕСТ 12: типичный счёт жильца — должен попасть в 3-15k диапазон
# ──────────────────────────────────────────────────────────────

def test_typical_resident_in_normal_range():
    """Средние тарифы + средний расход → счёт в реальном диапазоне 3-15k.
    Если этот тест начнёт падать после изменения тарифа/формулы —
    это сигнал, что что-то пошло не так в калькуляторе."""
    user = FakeUser(residents=2)
    room = FakeRoom(area=50.0, total_residents=2)
    tariff = FakeTariff()  # стандартные тарифы из фикстуры
    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("5"),
        volume_cold=Decimal("8"),
        volume_sewage=Decimal("13"),
        volume_electricity_share=Decimal("250"),
    )
    assert Decimal("3000") <= result["total_cost"] <= Decimal("15000"), (
        f"total_cost={result['total_cost']} вне разумного диапазона 3-15k ₽"
    )
    assert result["sanity_warning"] is None


# ──────────────────────────────────────────────────────────────
# ТЕСТЫ: meters_001_per_user_config — нормативы для жильцов без счётчиков
# ──────────────────────────────────────────────────────────────

def test_missing_hot_meter_uses_norm():
    """Жилец без ГВС-счётчика (has_hw_meter=False) — расход = норматив × жильцов.
    Передаём volume_hot=0, но в результате должен быть посчитан расход по нормативу.
    """
    # 3 жильца семьи без ГВС-счётчика: норматив × residents_count (для семьи
    # paying_residents = User.residents_count, см. calculations.paying_residents).
    user = FakeUser(residents=3, has_hw_meter=False)
    room = FakeRoom(area=30.0, total_residents=3)
    tariff = FakeTariff(hw_norm_per_capita="2.500")  # 2.5 м³/чел/мес

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("0"),     # клиент не подал — счётчика нет
        volume_cold=Decimal("4.0"),
        volume_sewage=Decimal("4.0"),
        volume_electricity_share=Decimal("50.0"),
    )

    # Норматив ГВС: 2.5 × 3 = 7.5 м³; ГВС = только water_heating=150 ₽/м³
    # (Bug AP/AQ: тариф уже включает воду, не суммируем с water_supply)
    # 7.5 × 150 = 1125.00 ₽
    assert result["cost_hot_water"] == Decimal("1125.00"), (
        f"ГВС по нормативу: ожидается 1125.00, получено {result['cost_hot_water']}"
    )
    # Канализация = (v_hot + v_cold) × sewage = (7.5 + 4) × 35 = 402.50 ₽
    assert result["cost_sewage"] == Decimal("402.50"), (
        f"Канализация после норматива: ожидается 402.50, получено {result['cost_sewage']}"
    )


def test_missing_meter_with_zero_norm_gives_zero_cost():
    """Если у жильца нет счётчика И норматив=0 — стоимость 0 (а не норматив × N)."""
    user = FakeUser(residents=2, has_el_meter=False)
    room = FakeRoom(area=20.0)
    # el_norm_per_capita по умолчанию 0
    tariff = FakeTariff()

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"),
        volume_cold=Decimal("4.0"),
        volume_sewage=Decimal("7.0"),
        volume_electricity_share=Decimal("100.0"),  # передан, но игнорируется
    )

    assert result["cost_electricity"] == Decimal("0.00"), (
        f"Свет без счётчика + норматив=0: ожидается 0, получено {result['cost_electricity']}"
    )


def test_no_meters_at_all_only_fixed_part_charged():
    """Жилец вообще без счётчиков и без нормативов — только фикс. часть."""
    user = FakeUser(
        residents=1, has_hw_meter=False, has_cw_meter=False, has_el_meter=False
    )
    room = FakeRoom(area=18.0)
    tariff = FakeTariff()  # нормативы по умолчанию 0

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
    # Фиксированные части (от площади) должны быть посчитаны
    assert result["cost_maintenance"] > Decimal("0.00")
    assert result["cost_social_rent"] > Decimal("0.00")


def test_present_meter_ignores_norm():
    """Если has_X_meter=True — норматив НЕ применяется, считается дельта счётчика.
    Это гарантия что новое поведение не ломает старое (по умолчанию все meters=True).
    """
    user = FakeUser(residents=2, has_hw_meter=True)  # счётчик есть
    room = FakeRoom(area=30.0)
    # norm=999 — большое число, чтобы убедиться что оно НЕ используется
    tariff = FakeTariff(hw_norm_per_capita="999.000")

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"),  # реальная подача
        volume_cold=Decimal("4.0"),
        volume_sewage=Decimal("7.0"),
        volume_electricity_share=Decimal("50.0"),
    )

    # ГВС от ФАКТА (3.0), а не норматива: 3.0 × 150 = 450.00 ₽
    # (Bug AQ: vol × water_heating, без сложения с water_supply)
    assert result["cost_hot_water"] == Decimal("450.00"), (
        f"При наличии счётчика норматив не применяется: получено {result['cost_hot_water']}"
    )


# ──────────────────────────────────────────────────────────────
# ТЕСТЫ СЕЗОННЫХ ПЕРЕКЛЮЧАТЕЛЕЙ
# Админ может выключить «отопительный сезон» или «подогрев ГВС»
# одним глобальным флагом — calculate_utilities обязан занулить
# соответствующие компоненты квитанции для всех жильцов сразу.
# ──────────────────────────────────────────────────────────────

def test_seasonal_heating_off_zeroes_fixed_part_heating():
    """heating_season_active=False → cost_fixed_part = 0 (ОДН удалён из
    системы, в фикс. части осталось только отопление)."""
    user = FakeUser(residents=2)
    room = FakeRoom(area=50.0)
    tariff = FakeTariff()  # heating=25.00

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"),
        volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"),
        volume_electricity_share=Decimal("100.0"),
        heating_season_active=False,
    )
    # cost_fixed_part = 50.00 × 0 = 0.00 (отопление off, ОДН удалён)
    assert result["cost_fixed_part"] == Decimal("0.00"), (
        f"При выключенном отоплении фикс. часть = 0, получили {result['cost_fixed_part']}"
    )


def test_seasonal_hot_water_heating_off_treats_hot_as_cold():
    """hot_water_heating_active=False → ГВС считается только за water_supply,
    как если бы вода была холодной. Полезно во время летней профилактики ТЭЦ."""
    user = FakeUser(residents=2)
    room = FakeRoom(area=50.0)
    tariff = FakeTariff()  # water_supply=40, water_heating=150

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"),
        volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"),
        volume_electricity_share=Decimal("100.0"),
        hot_water_heating_active=False,
    )
    # 3.0 × (40 + 0) = 120.00 (без подогрева)
    assert result["cost_hot_water"] == Decimal("120.00"), (
        f"При выключенном подогреве ГВС: 3.0 × 40 = 120.00, "
        f"получили {result['cost_hot_water']}"
    )


def test_seasonal_both_off_still_calculates_rest():
    """Если оба флага off — остальные статьи (вода, электр., содержание)
    считаются как обычно. FAIL-LOUD не срабатывает, потому что
    остаются ненулевые ставки."""
    user = FakeUser(residents=2)
    room = FakeRoom(area=50.0)
    tariff = FakeTariff()

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"),
        volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"),
        volume_electricity_share=Decimal("100.0"),
        heating_season_active=False,
        hot_water_heating_active=False,
    )
    # ГВС без подогрева, отопление 0, остальное по-обычному
    assert result["cost_hot_water"] == Decimal("120.00")
    # fixed_part = 0 (отопление off, ОДН удалён из системы)
    assert result["cost_fixed_part"] == Decimal("0.00")
    # cost_electricity не затронут
    assert result["cost_electricity"] == Decimal("550.00")  # 100 × 5.50


def test_seasonal_off_does_not_raise_on_heating_only_tariff():
    """Регрессионный тест: если в тарифе только heating ненулевой, и
    админ выключает отопление — функция НЕ должна падать CalculationError.
    Зануление сезонных применяется после FAIL-LOUD-проверки."""
    user = FakeUser(residents=2)
    room = FakeRoom(area=50.0)
    # Все ставки 0, кроме heating
    tariff = FakeTariff(
        water_supply="0", water_heating="0", sewage="0",
        electricity_rate="0", maintenance_repair="0",
        social_rent="0", waste_disposal="0",
        heating="25.00",  # единственная ненулевая
        electricity_per_sqm="0",
    )

    # FAIL-LOUD прошёл (heating ненулевой) → дальше зануляем сезонно → всё 0
    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("0"), volume_cold=Decimal("0"),
        volume_sewage=Decimal("0"), volume_electricity_share=Decimal("0"),
        heating_season_active=False,
    )
    assert result["cost_fixed_part"] == Decimal("0.00")
    assert result["total_cost"] == Decimal("0.00")


def test_seasonal_defaults_preserve_legacy_behavior():
    """Без указания флагов поведение калькулятора не меняется — флаги
    дефолтятся в True. Гарантия обратной совместимости для тестов,
    которые не передают новые параметры."""
    user = FakeUser(residents=2)
    room = FakeRoom(area=50.0)
    tariff = FakeTariff()

    result_default = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"), volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"), volume_electricity_share=Decimal("100.0"),
    )
    result_explicit = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"), volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"), volume_electricity_share=Decimal("100.0"),
        heating_season_active=True, hot_water_heating_active=True,
    )
    assert result_default["total_cost"] == result_explicit["total_cost"]


# ──────────────────────────────────────────────────────────────
# ТЕСТЫ ХОЛОСТЯЦКИХ КВАРТИР (Bug AS)
# ──────────────────────────────────────────────────────────────

def test_singles_apartment_meters_split_by_actual_residents():
    """Холостяцкая квартира: счётчики (ГВС/ХВС/канализация) делятся на
    ФАКТ. число жильцов; электричество приходит уже долей и НЕ делится
    повторно. area-based статьи считаются по max_capacity (не по факту)."""
    user = FakeUser(residents=1)
    # факт. жильцов = 3, макс. вместимость квартиры = 4 (разные числа!)
    room = FakeRoom(area=50.0, total_residents=3, is_singles_apartment=True, max_capacity=4)
    tariff = FakeTariff()  # все skip-флаги False по умолчанию

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"),       # 3 × 150 = 450, /3 факт = 150
        volume_cold=Decimal("5.0"),      # 5 × 40 = 200, /3 = 66.67
        volume_sewage=Decimal("8.0"),    # 8 × 35 = 280, /3 = 93.33
        volume_electricity_share=Decimal("90.0"),  # уже доля → 90 × 5.50 = 495, НЕ делим
    )
    assert result["cost_hot_water"] == Decimal("150.00"), result["cost_hot_water"]
    assert result["cost_cold_water"] == Decimal("66.67"), result["cost_cold_water"]
    assert result["cost_sewage"] == Decimal("93.33"), result["cost_sewage"]
    # Электричество НЕ делится повторно (иначе было бы /N²): 90 × 5.50 = 495.00
    assert result["cost_electricity"] == Decimal("495.00"), result["cost_electricity"]
    # area-based по max_capacity=4: Содержание 50/4 × 30.50 = 12.5 × 30.50 = 381.25
    assert result["cost_maintenance"] == Decimal("381.25"), result["cost_maintenance"]
    # Наём: 50/4 × 5.10 = 12.5 × 5.10 = 63.75
    assert result["cost_social_rent"] == Decimal("63.75"), result["cost_social_rent"]


def test_singles_area_based_uses_max_capacity_not_actual():
    """area-based статьи холостяка считаются от max_capacity и НЕ зависят
    от того, сколько фактически живёт. И Умнову, и Меликову (живут вдвоём,
    вместимость 4) начисляется одна и та же сумма за наём/ТКО/отопление."""
    tariff = FakeTariff()
    # Та же квартира, та же вместимость 4, но факт. жильцов 2 vs 4 — наём
    # обязан совпасть (зависит только от max_capacity).
    room_two = FakeRoom(area=43.10, total_residents=2, is_singles_apartment=True, max_capacity=4)
    room_full = FakeRoom(area=43.10, total_residents=4, is_singles_apartment=True, max_capacity=4)

    common = dict(
        volume_hot=Decimal("0"), volume_cold=Decimal("0"),
        volume_sewage=Decimal("0"), volume_electricity_share=Decimal("0"),
    )
    r_two = calculate_utilities(user=FakeUser(residents=1), room=room_two, tariff=tariff, **common)
    r_full = calculate_utilities(user=FakeUser(residents=1), room=room_full, tariff=tariff, **common)

    # Наём: 43.10 / 4 × 5.10 = 10.775 × 5.10 = 54.9525 → 54.95
    assert r_two["cost_social_rent"] == Decimal("54.95"), r_two["cost_social_rent"]
    assert r_two["cost_social_rent"] == r_full["cost_social_rent"], (
        "Наём холостяка не должен зависеть от фактического числа жильцов"
    )
    # ТКО: 10.775 × 6.50 = 70.0375 → 70.04
    assert r_two["cost_waste"] == Decimal("70.04"), r_two["cost_waste"]
    # Отопление: 10.775 × 25.00 = 269.375 → 269.38
    assert r_two["cost_fixed_part"] == Decimal("269.38"), r_two["cost_fixed_part"]


def test_singles_apartment_skip_flags_zero_components():
    """Если у тарифа стоит singles_skip_*, эти статьи не начисляются вообще."""
    user = FakeUser(residents=1)
    room = FakeRoom(area=50.0, total_residents=2, is_singles_apartment=True, max_capacity=2)
    tariff = FakeTariff(
        singles_skip_social_rent=True,   # 205 не начисляем
        singles_skip_heating=True,       # отопление тоже
    )

    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("2.0"),
        volume_cold=Decimal("4.0"),
        volume_sewage=Decimal("6.0"),
        volume_electricity_share=Decimal("50.0"),
    )
    assert result["cost_social_rent"] == Decimal("0.00"), "наём должен быть 0"
    # cost_fixed_part: отопление skip + ОДН удалён из системы → 0.00
    assert result["cost_fixed_part"] == Decimal("0.00"), result["cost_fixed_part"]
    # Содержание не skip — area-based по max_capacity=2: 50/2 × 30.50 = 762.50
    assert result["cost_maintenance"] == Decimal("762.50"), result["cost_maintenance"]


def test_singles_apartment_off_keeps_legacy_behavior():
    """Если is_singles_apartment=False — деления и skip-флагов нет."""
    user = FakeUser(residents=1)
    room_legacy = FakeRoom(area=50.0, total_residents=3, is_singles_apartment=False)
    room_singles = FakeRoom(area=50.0, total_residents=3, is_singles_apartment=True)
    tariff = FakeTariff()

    r_legacy = calculate_utilities(
        user=user, room=room_legacy, tariff=tariff,
        volume_hot=Decimal("3.0"), volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"), volume_electricity_share=Decimal("90.0"),
    )
    r_singles = calculate_utilities(
        user=user, room=room_singles, tariff=tariff,
        volume_hot=Decimal("3.0"), volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"), volume_electricity_share=Decimal("90.0"),
    )
    # legacy — обычная сумма; singles — поровну на 3 → меньше
    assert r_legacy["total_cost"] > r_singles["total_cost"]
    # Конкретно ГВС: legacy 450, singles 150 (450/3).
    assert r_legacy["cost_hot_water"] == Decimal("450.00")
    assert r_singles["cost_hot_water"] == Decimal("150.00")


# ──────────────────────────────────────────────────────────────
# ТЕСТЫ ТАРИФА «ЧТО НАЧИСЛЯЕТСЯ» (Bug AT)
# ──────────────────────────────────────────────────────────────

def test_charge_flags_default_true_legacy_behavior():
    """По умолчанию все charge_* = True — расчёт как раньше."""
    user = FakeUser()
    room = FakeRoom(area=50.0)
    tariff = FakeTariff()
    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"), volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"), volume_electricity_share=Decimal("100.0"),
    )
    # Все компоненты должны быть ненулевые при ненулевых тарифах/объёмах.
    assert result["cost_hot_water"] > 0
    assert result["cost_cold_water"] > 0
    assert result["cost_maintenance"] > 0
    assert result["cost_social_rent"] > 0


def test_charge_rent_only_preset():
    """Пресет «Только наём»: только cost_social_rent ненулевой.
    Фикс. часть = 0 (heating off; ОДН удалён из системы 29.05.2026)."""
    user = FakeUser()
    room = FakeRoom(area=50.0)
    tariff = FakeTariff(
        charge_hot_water=False, charge_cold_water=False,
        charge_sewage=False, charge_electricity=False,
        charge_maintenance=False, charge_social_rent=True,
        charge_heating=False, charge_waste=False,
    )
    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"), volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"), volume_electricity_share=Decimal("100.0"),
    )
    assert result["cost_hot_water"] == Decimal("0.00")
    assert result["cost_cold_water"] == Decimal("0.00")
    assert result["cost_sewage"] == Decimal("0.00")
    assert result["cost_electricity"] == Decimal("0.00")
    assert result["cost_maintenance"] == Decimal("0.00")
    assert result["cost_waste"] == Decimal("0.00")
    assert result["cost_fixed_part"] == Decimal("0.00")  # heating off, ОДН=0
    # Только наём: 50 × 5.10 = 255.00
    assert result["cost_social_rent"] == Decimal("255.00")
    assert result["total_cost"] == Decimal("255.00")


def test_charge_no_meters_preset():
    """Пресет «Без счётчиков»: 4 meter-флага false, остальные нормально."""
    user = FakeUser()
    room = FakeRoom(area=50.0)
    tariff = FakeTariff(
        charge_hot_water=False, charge_cold_water=False,
        charge_sewage=False, charge_electricity=False,
    )
    result = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("3.0"), volume_cold=Decimal("5.0"),
        volume_sewage=Decimal("8.0"), volume_electricity_share=Decimal("100.0"),
    )
    # Все 4 meter-cost = 0.
    assert result["cost_hot_water"] == Decimal("0.00")
    assert result["cost_cold_water"] == Decimal("0.00")
    assert result["cost_sewage"] == Decimal("0.00")
    assert result["cost_electricity"] == Decimal("0.00")
    # Площадь-компоненты на месте.
    assert result["cost_maintenance"] > 0
    assert result["cost_social_rent"] > 0


# ──────────────────────────────────────────────────────────────
# Регресс (ревизия #4): корректировка водоотведения и charge-гейт v_sew
# ──────────────────────────────────────────────────────────────

def test_sewage_correction_applied():
    """sewage_correction вычитается из объёма водоотведения, а не теряется.
    Регрессия: после правки #4 v_sew игнорировал volume_sewage (куда caller
    зашивал корректировку) → водоотведение завышалось. Теперь — явный параметр."""
    user = FakeUser(residents=2)
    room = FakeRoom(area=50.0, total_residents=2)
    tariff = FakeTariff(sewage="35.00")

    # ГВС 8 + ХВС 4 = 12; корректировка 3 → объём 9 → 9 × 35 = 315.00
    res = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("8"), volume_cold=Decimal("4"),
        volume_sewage=Decimal("12"), volume_electricity_share=Decimal("0"),
        sewage_correction=Decimal("3"),
    )
    assert res["cost_sewage"] == quantize_money(Decimal("9") * Decimal("35.00"))

    # corr=0 → полный объём 12 × 35 = 420.00 (без регрессии)
    res0 = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("8"), volume_cold=Decimal("4"),
        volume_sewage=Decimal("12"), volume_electricity_share=Decimal("0"),
    )
    assert res0["cost_sewage"] == quantize_money(Decimal("12") * Decimal("35.00"))

    # корректировка больше объёма → clamp в 0 (не отрицательная сумма)
    res_clamp = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("8"), volume_cold=Decimal("4"),
        volume_sewage=Decimal("12"), volume_electricity_share=Decimal("0"),
        sewage_correction=Decimal("100"),
    )
    assert res_clamp["cost_sewage"] == Decimal("0.00")


def test_sewage_excludes_uncharged_resource():
    """Аудит #4: при charge_hot_water=False объём ГВС НЕ течёт в водоотведение."""
    user = FakeUser(residents=2)
    room = FakeRoom(area=50.0, total_residents=2)
    tariff = FakeTariff(sewage="35.00", charge_hot_water=False)

    res = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=Decimal("8"), volume_cold=Decimal("4"),
        volume_sewage=Decimal("12"), volume_electricity_share=Decimal("0"),
    )
    # ГВС не начисляется → водоотведение только по ХВС: 4 × 35 = 140.00
    assert res["cost_sewage"] == quantize_money(Decimal("4") * Decimal("35.00"))
    assert res["cost_hot_water"] == Decimal("0.00")


# ──────────────────────────────────────────────────────────────
# Замена счётчика: is_meaningful_prev (METER_CLOSED ≠ prev, METER_REPLACEMENT = prev)
# ──────────────────────────────────────────────────────────────

def test_meter_replacement_prev_flags():
    """METER_CLOSED (финал старого счётчика, большое значение) НЕ годится как prev
    → не блокирует новую малую подачу. METER_REPLACEMENT (новый baseline) годится."""
    from app.modules.utility.services.reading_calculator import is_meaningful_prev

    class _R:
        def __init__(self, flags):
            self.anomaly_flags = flags

    assert is_meaningful_prev(_R("METER_CLOSED")) is False
    assert is_meaningful_prev(_R("METER_REPLACEMENT")) is True
    assert is_meaningful_prev(_R(None)) is True            # обычная реальная подача
    assert is_meaningful_prev(_R("AUTO_NORM")) is False    # синтетика (контроль)


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
    test_calculate_passes_when_one_rate_nonzero()
    test_sanity_warning_for_huge_total()
    test_no_sanity_warning_for_normal_total()
    test_per_capita_has_same_keys()
    test_per_capita_no_longer_routed()
    test_costs_for_model_fields_filters_meta()
    test_typical_resident_in_normal_range()
    test_missing_hot_meter_uses_norm()
    test_missing_meter_with_zero_norm_gives_zero_cost()
    test_no_meters_at_all_only_fixed_part_charged()
    test_present_meter_ignores_norm()
    # test_calculate_raises_when_all_rates_zero — pytest.raises только под pytest
    print("\n🎉 Все тесты пройдены успешно!")
