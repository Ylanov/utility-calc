"""Unit-тесты для парсеров debt_import.py — без БД, чисто текстовые.

Покрываем:
  - parse_contract_line:  разбор строки «Договор от ... № ...» из ОСВ 1С
  - pick_saldo_value:     выбор актуального сальдо (конец vs начало)
"""
from datetime import date
from decimal import Decimal

from app.modules.utility.services.debt_import import (
    parse_contract_line,
    pick_saldo_pair,
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


class TestPickSaldoPair:
    """Парная логика выбора сальдо — корректно работает когда жилец
    переплатил или у него «переехал» долг в переплату.

    Колонки в стандартной ОСВ:
      4 = Сальдо начало - Дебет
      7 = Сальдо начало - Кредит
      10 = Обороты - Дебет
      13 = Обороты - Кредит
      16 = Сальдо конец - Дебет
      19 = Сальдо конец - Кредит
    """

    def test_glob_case_overpay_no_debt(self):
        """REGRESSION: Глоба заплатил больше долга → переплата.

        Сальдо начало Дебет = 10908.10
        Обороты Кредит      = 18000     (заплатил)
        Сальдо конец Дебет  = пусто     (долга нет)
        Сальдо конец Кредит = 7091.90   (переплата)

        Раньше pick_saldo_value(end=16, start=4) брал fallback → 10908.10
        как «долг». Неверно: у Глобы НЕТ долга, есть переплата.
        """
        row = (
            "Глоба", None, None, None,
            10908.10,  # 4: Сальдо начало Дебет
            None, None,
            None,  # 7: Сальдо начало Кредит (пусто)
            None, None,
            None,  # 10: Обороты Дебет (пусто)
            None, None,
            18000,  # 13: Обороты Кредит = заплатил
            None, None,
            None,  # 16: Сальдо конец Дебет (пусто — долга нет)
            None, None,
            7091.90,  # 19: Сальдо конец Кредит = переплата
        )
        debt, over = pick_saldo_pair(
            row,
            end_debit_col=16, end_credit_col=19,
            start_debit_col=4, start_credit_col=7,
        )
        assert debt == Decimal("0"), f"У Глобы НЕ должно быть долга, получено {debt}"
        assert over == Decimal("7091.90")

    def test_malyshkin_case_both_debt_and_overpay(self):
        """Малышкин: на конец одновременно долг и переплата (разные субсчета)."""
        row = (
            "Малышкин", None, None, None,
            4627.08,  # 4
            None, None,
            None,  # 7
            None, None,
            None,  # 10
            None, None,
            None,  # 13
            None, None,
            4631.33,  # 16: Сальдо конец Дебет
            None, None,
            4.25,  # 19: Сальдо конец Кредит
        )
        debt, over = pick_saldo_pair(
            row,
            end_debit_col=16, end_credit_col=19,
            start_debit_col=4, start_credit_col=7,
        )
        assert debt == Decimal("4631.33")
        assert over == Decimal("4.25")

    def test_no_turnover_fallback_to_start(self):
        """Жилец без оборотов: обе ячейки конца пустые → берём начало."""
        row = (
            "Иванов", None, None, None,
            500,  # 4: Сальдо начало Дебет
            None, None,
            None,  # 7
            None, None,
            None,  # 10
            None, None,
            None,  # 13
            None, None,
            None,  # 16: end Дебет пуст
            None, None,
            None,  # 19: end Кредит пуст
        )
        debt, over = pick_saldo_pair(
            row,
            end_debit_col=16, end_credit_col=19,
            start_debit_col=4, start_credit_col=7,
        )
        assert debt == Decimal("500")
        assert over == Decimal("0")

    def test_debt_closed_explicit_zero(self):
        """Долг закрыт: в конец Дебет явный 0 (а не None), оба = 0."""
        row = (
            "Петров", None, None, None,
            1000,  # начало Дебет
            None, None,
            None,
            None, None,
            None, None, None,
            None, None, None,
            0,  # 16: конец Дебет = 0 явный (долг закрыт)
            None, None,
            None,  # 19: конец Кредит
        )
        debt, over = pick_saldo_pair(
            row,
            end_debit_col=16, end_credit_col=19,
            start_debit_col=4, start_credit_col=7,
        )
        # has_end_data=True (потому что end_d=0 НЕ None)
        # debt = 0 (явно закрыт), over = 0 (None)
        assert debt == Decimal("0")
        assert over == Decimal("0")

    def test_both_empty_returns_zero_pair(self):
        row = ("ФИО",)
        debt, over = pick_saldo_pair(
            row,
            end_debit_col=16, end_credit_col=19,
            start_debit_col=4, start_credit_col=7,
        )
        assert debt == Decimal("0")
        assert over == Decimal("0")

    def test_simple_two_column_layout(self):
        """Упрощённый отчёт с одной парой Дебет/Кредит — start_col == end_col."""
        row = ("ФИО", None, None, None, 800, None, None, 50)
        debt, over = pick_saldo_pair(
            row,
            end_debit_col=4, end_credit_col=7,
            start_debit_col=4, start_credit_col=7,
        )
        assert debt == Decimal("800")
        assert over == Decimal("50")
