# app/tests/test_recent_features.py
#
# Юнит-тесты для нового/критичного кода (июнь 2026): QR-портал, объединённый
# реестр, security-фиксы. Чистые функции — без БД, быстро. Защита от регрессий.

from decimal import Decimal

import pytest


# ──────────────────────────────────────────────────────────────
# like_contains — экранирование LIKE-инъекции (security-аудит #6/7/14-17)
# ──────────────────────────────────────────────────────────────
from app.modules.utility.services.search_utils import like_contains


@pytest.mark.parametrize("inp, expected", [
    ("иванов", "%иванов%"),
    ("a%b", "%ab%"),        # % из ввода удаляется (не метасимвол)
    ("a_b", "%ab%"),        # _ удаляется
    ("100%_x", "%100x%"),
    ("", "%%"),
    (None, "%%"),
    ("комната 101", "%комната 101%"),
])
def test_like_contains_strips_wildcards(inp, expected):
    assert like_contains(inp) == expected


def test_like_contains_no_wildcards_remain():
    out = like_contains("%%__%%abc__")
    assert "%" not in out[1:-1]   # внутри (между обрамляющими %) нет wildcard
    assert "_" not in out
    assert out == "%abc%"


# ──────────────────────────────────────────────────────────────
# _normalize_saldo — защита от отрицательного долга в импорте ОСВ (#5)
# ──────────────────────────────────────────────────────────────
from app.modules.utility.services.debt_import import _normalize_saldo


def test_normalize_saldo_single_column_unchanged():
    # Легитимные одностолбцовые строки НЕ меняются.
    assert _normalize_saldo(Decimal("100.00"), Decimal("0")) == (Decimal("100.00"), Decimal("0"))
    assert _normalize_saldo(Decimal("0"), Decimal("50.00")) == (Decimal("0"), Decimal("50.00"))


def test_normalize_saldo_negative_debit_becomes_overpayment():
    # Отрицательное Дт-сальдо из битого ОСВ → переплата (а не отрицательный долг).
    debt, over = _normalize_saldo(Decimal("-100.00"), Decimal("0"))
    assert debt == Decimal("0")
    assert over == Decimal("100.00")
    assert debt >= 0 and over >= 0


def test_normalize_saldo_both_columns_netted():
    # Обе колонки заполнены → нетируем Дт − Кр.
    assert _normalize_saldo(Decimal("100"), Decimal("40")) == (Decimal("60"), Decimal("0"))
    assert _normalize_saldo(Decimal("30"), Decimal("80")) == (Decimal("0"), Decimal("50"))


def test_normalize_saldo_zero():
    assert _normalize_saldo(Decimal("0"), Decimal("0")) == (Decimal("0"), Decimal("0"))


def test_normalize_saldo_never_negative():
    # Инвариант: оба значения всегда >= 0 при любом вводе.
    for d, o in [("-5", "-5"), ("-100", "3"), ("7", "-7"), ("0", "-1")]:
        debt, over = _normalize_saldo(Decimal(d), Decimal(o))
        assert debt >= 0 and over >= 0


# ──────────────────────────────────────────────────────────────
# _reading_source — источник боевого показания по anomaly_flags (реестр)
# ──────────────────────────────────────────────────────────────
from app.modules.utility.routers.admin_registry import _reading_source


@pytest.mark.parametrize("flags, src", [
    ("GSHEETS_AUTO", "gsheets"),
    ("GSHEETS_AUTO_BASELINE", "gsheets"),
    ("MANUAL_RECEIPT", "manual"),
    ("AUTO_NORM", "auto"),
    ("AUTO_AVG_FALLBACK", "auto"),
    ("STATIC_RENT", "auto"),
    ("PENDING", "user"),
    ("BASELINE", "user"),       # первая подача жильца — это user, не auto
    ("PENDING|SINGLES_SHARED", "user"),
    ("", "user"),
    (None, "user"),
])
def test_reading_source(flags, src):
    code, label = _reading_source(flags)
    assert code == src
    assert isinstance(label, str) and label


# ──────────────────────────────────────────────────────────────
# generate_qr_token — неугадываемый токен квартиры (QR-портал)
# ──────────────────────────────────────────────────────────────
from app.modules.utility.services.qr_portal import generate_qr_token


def test_qr_token_strong_and_unique():
    a = generate_qr_token()
    b = generate_qr_token()
    assert isinstance(a, str)
    assert len(a) >= 40                      # 32 байта → ~43 url-safe символа
    assert a != b                            # каждый вызов уникален
    # url-safe алфавит (без +, /, =)
    import re
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", a)
