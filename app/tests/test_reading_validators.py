"""Тесты на reading_validators.py — единый source of truth для валидации
показаний счётчиков. См. инцидент мая 2026 (1.48 млрд ₽ на дашборде).
"""
from decimal import Decimal

from app.modules.utility.services.reading_validators import (
    MAX_ELECTRICITY_METER_VALUE,
    MAX_TOTAL_COST_PER_READING,
    MAX_WATER_METER_VALUE,
    validate_meter_reading,
    validate_total_cost,
)


# ============================================================
# ABSOLUTE THRESHOLD (overflow)
# ============================================================

def test_overflow_hot_water_blocks():
    """Главный кейс — пропущенная точка в показании счётчика."""
    r = validate_meter_reading(
        hot=Decimal("1427957"),  # реальный кейс из прода (id=403, user=169)
        cold=Decimal("1482837"),
        elect=Decimal("0"),
        is_baseline=True,
    )
    assert not r.ok
    assert any("hot_water" in e for e in r.errors)
    assert any("cold_water" in e for e in r.errors)


def test_overflow_just_over_threshold():
    """Edge: ровно на 1 больше порога — должен блочить."""
    r = validate_meter_reading(
        hot=MAX_WATER_METER_VALUE + Decimal("1"),
        cold=Decimal("100"),
        elect=Decimal("0"),
        is_baseline=True,
    )
    assert not r.ok
    assert any("hot_water" in e for e in r.errors)


def test_at_threshold_passes():
    """Edge: ровно на пороге — проходит."""
    r = validate_meter_reading(
        hot=MAX_WATER_METER_VALUE,
        cold=Decimal("100"),
        elect=Decimal("0"),
        is_baseline=True,
    )
    assert r.ok, f"errors: {r.errors}"


def test_overflow_electricity_blocks():
    r = validate_meter_reading(
        hot=Decimal("100"),
        cold=Decimal("200"),
        elect=MAX_ELECTRICITY_METER_VALUE + Decimal("1"),
        is_baseline=True,
    )
    assert not r.ok
    assert any("electricity" in e for e in r.errors)


# ============================================================
# NULL / NEGATIVE
# ============================================================

def test_negative_value_blocks():
    r = validate_meter_reading(
        hot=Decimal("-1"),
        cold=Decimal("100"),
        elect=Decimal("50"),
        is_baseline=True,
    )
    assert not r.ok
    assert any("отрицательным" in e for e in r.errors)


def test_null_water_blocks():
    """hot/cold обязательны — gsheets без них = битая строка."""
    r = validate_meter_reading(
        hot=None,
        cold=Decimal("100"),
        elect=Decimal("0"),
        is_baseline=True,
    )
    assert not r.ok
    assert any("hot_water не задан" in e for e in r.errors)


def test_null_electricity_ok():
    """elect=None допустимо — gsheets его не передаёт по дизайну."""
    r = validate_meter_reading(
        hot=Decimal("100"),
        cold=Decimal("200"),
        elect=None,
        is_baseline=True,
    )
    assert r.ok, f"errors: {r.errors}"


# ============================================================
# MONOTONICITY
# ============================================================

def test_decreasing_blocks_when_not_baseline():
    """Счётчик не может уменьшаться (ситуация замены счётчика — отдельный flow)."""
    r = validate_meter_reading(
        hot=Decimal("99"),
        cold=Decimal("199"),
        elect=Decimal("49"),
        prev_hot=Decimal("100"),
        prev_cold=Decimal("200"),
        prev_elect=Decimal("50"),
        is_baseline=False,
    )
    assert not r.ok
    assert any("меньше предыдущего" in e for e in r.errors)


def test_decreasing_allowed_when_baseline():
    """is_baseline=True — сравнения нет (предыдущие могут быть «грязные»)."""
    r = validate_meter_reading(
        hot=Decimal("50"),  # меньше prev — но baseline = OK
        cold=Decimal("100"),
        elect=Decimal("25"),
        prev_hot=Decimal("100"),
        prev_cold=Decimal("200"),
        prev_elect=Decimal("50"),
        is_baseline=True,
    )
    assert r.ok, f"errors: {r.errors}"


def test_equal_values_pass():
    """Равные показания (нулевой расход) — это валидно."""
    r = validate_meter_reading(
        hot=Decimal("100"),
        cold=Decimal("200"),
        elect=Decimal("50"),
        prev_hot=Decimal("100"),
        prev_cold=Decimal("200"),
        prev_elect=Decimal("50"),
        is_baseline=False,
    )
    assert r.ok, f"errors: {r.errors}"


# ============================================================
# DELTA SANITY (warnings, not errors)
# ============================================================

def test_huge_water_delta_warns():
    """Расход 500 м³/мес — warning, не error (редкие сценарии возможны)."""
    r = validate_meter_reading(
        hot=Decimal("700"),
        cold=Decimal("100"),
        elect=Decimal("50"),
        prev_hot=Decimal("100"),  # дельта = 600 м³ — катастрофа
        prev_cold=Decimal("100"),
        prev_elect=Decimal("50"),
        is_baseline=False,
    )
    assert r.ok, f"errors: {r.errors}"
    assert any("hot_water" in w for w in r.warnings)


def test_huge_electricity_delta_warns():
    r = validate_meter_reading(
        hot=Decimal("100"),
        cold=Decimal("200"),
        elect=Decimal("10000"),  # дельта 9950 кВт — много
        prev_hot=Decimal("100"),
        prev_cold=Decimal("200"),
        prev_elect=Decimal("50"),
        is_baseline=False,
    )
    assert r.ok
    assert any("electricity" in w for w in r.warnings)


def test_normal_consumption_no_warnings():
    """10 м³ воды + 200 кВт за месяц — типичный жилец без warnings."""
    r = validate_meter_reading(
        hot=Decimal("105"),
        cold=Decimal("210"),
        elect=Decimal("250"),
        prev_hot=Decimal("100"),
        prev_cold=Decimal("200"),
        prev_elect=Decimal("50"),
        is_baseline=False,
    )
    assert r.ok
    assert not r.warnings


# ============================================================
# total_cost SANITY
# ============================================================

def test_total_cost_huge_blocks():
    r = validate_total_cost(MAX_TOTAL_COST_PER_READING + Decimal("1"))
    assert not r.ok


def test_total_cost_normal_passes():
    r = validate_total_cost(Decimal("8500"))
    assert r.ok


def test_total_cost_at_threshold_passes():
    r = validate_total_cost(MAX_TOTAL_COST_PER_READING)
    assert r.ok


def test_total_cost_none_passes():
    """None означает «не считали» — не наша проблема."""
    r = validate_total_cost(None)
    assert r.ok


# ============================================================
# CONTRACT
# ============================================================

def test_validation_result_bool():
    """ValidationResult должен работать как bool (для удобства caller'ов)."""
    r_ok = validate_meter_reading(
        hot=Decimal("100"), cold=Decimal("200"), elect=Decimal("50"),
        is_baseline=True,
    )
    r_fail = validate_meter_reading(
        hot=Decimal("9999999"), cold=Decimal("100"), elect=Decimal("0"),
        is_baseline=True,
    )
    assert bool(r_ok) is True
    assert bool(r_fail) is False
    if not r_fail:
        pass  # синтаксис должен работать
