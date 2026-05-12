"""Unit-тесты для парсеров debt_import.py — без БД, чисто текстовые.

Покрываем:
  - parse_contract_line:  разбор строки «Договор от ... № ...» из ОСВ 1С
  - pick_saldo_value:     выбор актуального сальдо (конец vs начало)
"""
from datetime import date
from decimal import Decimal

from app.modules.utility.services.debt_import import (
    parse_contract_line,
    pick_saldo_value,
)


class TestParseContractLine:
    def test_format_date_first(self):
        """«Договор от 14.02.2017 № 1013» — date потом number."""
        result = parse_contract_line("Договор от 14.02.2017 № 1013")
        assert result is not None
        assert result["number"] == "1013"
        assert result["signed_date"] == date(2017, 2, 14)

    def test_format_number_first(self):
        """«Договор № 923 от 28.12.2015» — number потом date."""
        result = parse_contract_line("Договор № 923 от 28.12.2015")
        assert result is not None
        assert result["number"] == "923"
        assert result["signed_date"] == date(2015, 12, 28)

    def test_format_bare_number(self):
        """«Договор 923 от 28.12.2015» — без «№»."""
        result = parse_contract_line("Договор 923 от 28.12.2015")
        assert result is not None
        assert result["number"] == "923"
        assert result["signed_date"] == date(2015, 12, 28)

    def test_number_with_letters(self):
        """«Договор от 07.02.2013 № 417-К» — буква в номере."""
        result = parse_contract_line("Договор от 07.02.2013 № 417-К")
        assert result is not None
        assert result["number"] == "417-К"
        assert result["signed_date"] == date(2013, 2, 7)

    def test_with_extra_whitespace(self):
        result = parse_contract_line("  Договор   от   01.07.2025  №  1745  ")
        assert result is not None
        assert result["number"] == "1745"
        assert result["signed_date"] == date(2025, 7, 1)

    def test_two_digit_year(self):
        """Если в 1С двузначный год — считаем 20XX."""
        result = parse_contract_line("Договор от 14.02.17 № 1013")
        assert result is not None
        assert result["signed_date"] == date(2017, 2, 14)

    def test_not_a_contract_returns_none(self):
        assert parse_contract_line("Иванов Иван Иванович") is None
        assert parse_contract_line("Сальдо на начало периода") is None
        assert parse_contract_line("Итого по счёту 209") is None
        assert parse_contract_line(None) is None
        assert parse_contract_line("") is None
        assert parse_contract_line("   ") is None

    def test_starts_with_dogovor_but_no_date(self):
        """Если в строке слово «Договор» но нет даты — не парсим."""
        assert parse_contract_line("Договор найма") is None

    def test_invalid_date(self):
        """Несуществующая дата (например 30.02.2017) — None, не падает."""
        result = parse_contract_line("Договор от 30.02.2017 № 1013")
        assert result is None

    def test_no_number_only_date(self):
        """«Договор от 14.02.2017» без номера — None."""
        assert parse_contract_line("Договор от 14.02.2017") is None


class TestPickSaldoValue:
    """Логика выбора актуального сальдо из строки ОСВ 1С.

    В ОСВ есть 3 секции: Сальдо начало | Обороты | Сальдо конец.
    Нужно брать Сальдо на КОНЕЦ периода — это текущее состояние долга.
    Если ячейка пустая (None) — fallback на Сальдо на начало (1С не
    повторяет неизменное значение).
    Если ячейка содержит явный 0 — берём 0 (долг закрыт).
    """

    def test_takes_end_when_present(self):
        """End-колонка имеет значение → берём её."""
        # row[4]=начало, row[7]=конец (стандарт ОСВ)
        row = ("ФИО", None, None, None, "100.00", None, None, "50.00")
        assert pick_saldo_value(row, end_col=7, start_col=4) == Decimal("50.00")

    def test_fallback_to_start_when_end_is_none(self):
        """End пустая → берём начало (жилец без оборотов в периоде)."""
        row = ("ФИО", None, None, None, "936.87", None, None, None)
        assert pick_saldo_value(row, end_col=7, start_col=4) == Decimal("936.87")

    def test_explicit_zero_at_end_means_closed(self):
        """End=0 (а не None) — долг закрыт. НЕ fallback на старое начало."""
        row = ("ФИО", None, None, None, "1000.00", None, None, 0)
        assert pick_saldo_value(row, end_col=7, start_col=4) == Decimal("0")

    def test_both_empty_returns_zero(self):
        row = ("ФИО", None, None, None, None, None, None, None)
        assert pick_saldo_value(row, end_col=7, start_col=4) == Decimal("0")

    def test_single_section_when_end_equals_start(self):
        """Упрощённый отчёт с одной парой Дебет/Кредит — end_col == start_col."""
        row = ("ФИО", None, None, None, "500.00")
        assert pick_saldo_value(row, end_col=4, start_col=4) == Decimal("500.00")

    def test_out_of_bounds_indices(self):
        """Индексы за границей row → 0 (защита от коротких строк)."""
        row = ("ФИО",)
        assert pick_saldo_value(row, end_col=7, start_col=4) == Decimal("0")

    def test_numeric_value_not_string(self):
        """В реальных Excel значения часто float/int, не string."""
        row = ("ФИО", None, None, None, 100.5, None, None, 75)
        assert pick_saldo_value(row, end_col=7, start_col=4) == Decimal("75")
