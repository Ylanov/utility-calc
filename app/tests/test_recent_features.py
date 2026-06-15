# app/tests/test_recent_features.py
#
# Юнит-тесты для нового/критичного кода (июнь 2026): QR-портал, объединённый
# реестр, security-фиксы. Чистые функции — без БД, быстро. Защита от регрессий.

import re
from decimal import Decimal

import pytest

from app.modules.utility.routers.admin_registry import _reading_source
from app.modules.utility.services.debt_import import _normalize_saldo
from app.modules.utility.services.excel_readings_import import (
    _is_junk_fio, _num, _sheet_kind, parse_readings_workbook,
)
from app.modules.utility.services.qr_portal import (
    QR_TICKET_SUBJECT, generate_qr_token, notify_reading_rejected,
)
from app.modules.utility.services.search_utils import like_contains


# ──────────────────────────────────────────────────────────────
# like_contains — экранирование LIKE-инъекции (security-аудит #6/7/14-17)
# ──────────────────────────────────────────────────────────────
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
def test_qr_token_strong_and_unique():
    a = generate_qr_token()
    b = generate_qr_token()
    assert isinstance(a, str)
    assert len(a) >= 40                      # 32 байта → ~43 url-safe символа
    assert a != b                            # каждый вызов уникален
    # url-safe алфавит (без +, /, =)
    assert re.fullmatch(r"[A-Za-z0-9_\-]+", a)


# ──────────────────────────────────────────────────────────────
# notify_reading_rejected — уведомление жильцу при отклонении показания
# ──────────────────────────────────────────────────────────────
class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)


def test_notify_rejected_creates_qr_ticket():
    db = _FakeDb()
    notify_reading_rejected(db, user_id=7, period_name="Июнь 2026", reason="не совпадает со счётчиком")
    assert len(db.added) == 1
    t = db.added[0]
    # Тема — QR-маркер: иначе /messages портала уведомление не отдаст,
    # а cleanup_qr_tickets_task не подчистит через 5 дней.
    assert t.subject == QR_TICKET_SUBJECT
    assert t.user_id == 7
    assert t.status == "answered"            # системное — отвечать не на что
    assert "Июнь 2026" in t.admin_response
    assert "не совпадает со счётчиком" in t.admin_response
    assert "заново" in t.admin_response      # призыв переподать


def test_notify_rejected_without_period_and_reason():
    db = _FakeDb()
    notify_reading_rejected(db, user_id=3)
    t = db.added[0]
    assert t.subject == QR_TICKET_SUBJECT
    assert "отклонены администратором" in t.admin_response
    assert "Причина" not in t.admin_response   # нет причины — нет пустой строки


# ──────────────────────────────────────────────────────────────
# excel_readings_import — парсер показаний из Excel (формат прев/текущий)
# ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("title, kind", [
    ("горячая", "hot"), ("Горячая вода", "hot"), ("ГВС", "hot"),
    ("холодная", "cold"), ("ХВС", "cold"),
    ("электричество", "elect"), ("Свет", "elect"),
    ("Лист2", None), ("", None), ("прочее", None),
])
def test_sheet_kind(title, kind):
    assert _sheet_kind(title) == kind


@pytest.mark.parametrize("inp, expected", [
    (None, None), ("", None), ("  ", None),
    (1466, Decimal("1466")), (12.5, Decimal("12.5")),
    ("845", Decimal("845")), ("1 234", Decimal("1234")), ("12,5", Decimal("12.5")),
    ("мусор", None),
])
def test_num_parse(inp, expected):
    assert _num(inp) == expected


@pytest.mark.parametrize("fio, junk", [
    (None, True), ("", True), ("0", True), ("Итого:", True),
    ("2 общежитие.", True), ("Этаж:", True), ("Ф.И.О.", True),
    ("123", True), ("---", True),
    ("Дронин Константин Николаевич", False), ("Оболенская Кира", False),
])
def test_is_junk_fio(fio, junk):
    assert _is_junk_fio(fio) is junk


def _make_workbook_bytes():
    """Двухлистовый Excel как в реальном файле: горячая + холодная,
    колонки ФИО|прев|тек, с мусорными строками и пустым текущим."""
    import io
    from openpyxl import Workbook
    wb = Workbook()
    hot = wb.active
    hot.title = "горячая"
    hot.append(["Ф.И.О.", "Предыдущий месяц", "Текущий месяц"])
    hot.append(["2 общежитие.", None, None])          # мусор-разделитель
    hot.append(["Итого:", None, None])                # мусор
    hot.append(["Иванов Иван Иванович", 100, 110])    # норм подача
    hot.append(["Петров Пётр", 50, None])             # не подал (пусто)
    hot.append(["0", 5, 5])                           # мусор-ФИО
    hot.append(["Сидоров Сидор Сидорович", 200, 180]) # откат счётчика
    cold = wb.create_sheet("холодная")
    cold.append(["Ф.И.О.", "Предыдущий месяц", "Текущий месяц"])
    cold.append(["Иванов Иван Иванович", 300, 320])
    cold.append(["Петров Пётр", 80, 85])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_readings_workbook():
    parsed = parse_readings_workbook(_make_workbook_bytes())
    people = parsed["people"]
    # 3 валидных человека (мусор/итого/0 отброшены).
    assert len(people) == 3
    assert set(parsed["meters_present"]) == {"hot", "cold"}
    assert parsed["skipped_rows"] >= 3   # 2 общежитие + Итого + 0 + заголовки

    # Иванов есть в обоих листах — объединён по нормализованному ключу.
    ivanov = next(v for v in people.values() if v["fio"].startswith("Иванов"))
    assert ivanov["hot"] == {"prev": Decimal("100"), "cur": Decimal("110")}
    assert ivanov["cold"] == {"prev": Decimal("300"), "cur": Decimal("320")}

    # Петров не подал ГВС (текущий пуст), но в холодной подал.
    petrov = next(v for v in people.values() if v["fio"].startswith("Петров"))
    assert petrov["hot"]["cur"] is None
    assert petrov["cold"]["cur"] == Decimal("85")

    # Сидоров — откат счётчика (только в горячей).
    sidorov = next(v for v in people.values() if v["fio"].startswith("Сидоров"))
    assert sidorov["hot"]["prev"] == Decimal("200")
    assert sidorov["hot"]["cur"] == Decimal("180")
