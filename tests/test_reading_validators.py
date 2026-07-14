# tests/test_reading_validators.py
"""Валидаторы подачи: строгий формат 5+3 (QR-портал) и санитарный потолок
итога (инцидент «1.48 млрд ₽ на пропущенных точках»)."""
from decimal import Decimal

from app.modules.utility.services.reading_validators import (
    validate_raw_format, validate_total_cost,
)


def test_strict_5_3_accepts_canonical():
    assert validate_raw_format("01427.957", "5_3_strict") is None
    assert validate_raw_format("00245.000", "5_3_strict") is None
    # Запятая как разделитель тоже допустима (нормализуется в точку).
    assert validate_raw_format("00245,000", "5_3_strict") is None


def test_strict_5_3_accepts_short_integer_part():
    # Паттерн ^\d{1,5}\.\d{3}$ — 1..5 целых цифр допустимы (ведущие нули
    # необязательны), строго 3 дробных.
    assert validate_raw_format("1427.957", "5_3_strict") is None
    assert validate_raw_format("5.000", "5_3_strict") is None


def test_strict_5_3_rejects_wrong_shapes():
    assert validate_raw_format("245", "5_3_strict") is not None           # нет дроби
    assert validate_raw_format("001427.957", "5_3_strict") is not None    # 6 целых
    assert validate_raw_format("01427.95", "5_3_strict") is not None      # 2 дробных
    assert validate_raw_format("01427.9571", "5_3_strict") is not None    # 4 дробных
    assert validate_raw_format("", "5_3_strict") is not None
    assert validate_raw_format(None, "5_3_strict") is not None


def test_free_formats_skip_validation():
    assert validate_raw_format("245", "any") is None
    assert validate_raw_format("245", "5_no_decimal") is None


def test_total_cost_ceiling():
    ok = validate_total_cost(Decimal("4335.78"))
    assert ok.ok and not ok.errors
    bad = validate_total_cost(Decimal("1480000000"))
    assert not bad.ok and bad.errors
    # None = «нечего проверять», не ошибка.
    assert validate_total_cost(None).ok
