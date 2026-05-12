"""Unit-тесты для парсеров debt_import.py — без БД, чисто текстовые.

Сейчас покрываем:
  - parse_contract_line: разбор строки «Договор от ... № ...» из ОСВ 1С
    (4 поддерживаемых формата + негативные кейсы)
"""
from datetime import date

from app.modules.utility.services.debt_import import parse_contract_line


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
