# tests/test_clean_decimal.py
"""clean_decimal — разбор денежных сумм из Excel 1С и ГИС ГМП.

Регрессия 2026-06-27 (commit 281059a): релей-экспорт ОСВ отдавал US-формат
«1,318.69», наивный replace(',', '.') давал «1.318.69» → 0.00 → ВСЕ долги
≥1000 обнулялись («98% по нулям»). Эти тесты закрепляют оба формата.
"""
from decimal import Decimal

from app.modules.utility.services.debt_import import clean_decimal


def test_us_format_thousands():
    # Регрессионный кейс бага: запятая — разряды, точка — дробь.
    assert clean_decimal("1,318.69") == Decimal("1318.69")
    assert clean_decimal("1,936,065.31") == Decimal("1936065.31")


def test_russian_format():
    assert clean_decimal("1 936 065,31") == Decimal("1936065.31")
    assert clean_decimal("2054,48") == Decimal("2054.48")


def test_nbsp_thousands():
    assert clean_decimal("6\xa0875,82") == Decimal("6875.82")


def test_plain_numbers():
    assert clean_decimal("6875.82") == Decimal("6875.82")
    assert clean_decimal("0") == Decimal("0")
    assert clean_decimal(1318.69) == Decimal("1318.69")
    assert clean_decimal(Decimal("42.10")) == Decimal("42.10")


def test_empty_and_garbage():
    assert clean_decimal(None) == Decimal("0")
    assert clean_decimal("") == Decimal("0")
    assert clean_decimal("—") == Decimal("0")


def test_negative():
    # Кредитовое сальдо в ОСВ может прийти со знаком.
    assert clean_decimal("-1,318.69") == Decimal("-1318.69")
