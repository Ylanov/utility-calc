"""Тесты parse_period_name / period_chron_key.

Покрытие основной фичи: хронологическая сортировка биллинговых периодов
по имени (а не по `BillingPeriod.id` или `created_at`). Это критично для
правильного расчёта дельт показаний у жильца, когда подачи импортируются
задним числом (см. инцидент мая 2026 с Сорокиным С.А. — Февральская
подача за май делала майскую дельту отрицательной).
"""
from app.modules.utility.services.period_helpers import (
    parse_period_name,
    period_chron_key,
)


def test_parse_basic():
    assert parse_period_name("Май 2026") == (2026, 5)
    assert parse_period_name("Февраль 2026") == (2026, 2)
    assert parse_period_name("Декабрь 2025") == (2025, 12)
    assert parse_period_name("Январь 2026") == (2026, 1)


def test_parse_case_insensitive():
    """Имена могут прийти с разным регистром (особенно после ручного ввода)."""
    assert parse_period_name("МАЙ 2026") == (2026, 5)
    assert parse_period_name("май 2026") == (2026, 5)
    assert parse_period_name("МаЙ 2026") == (2026, 5)


def test_parse_extra_whitespace():
    """Пользователь мог накосячить с пробелами при создании периода вручную."""
    assert parse_period_name("  Май   2026  ") == (2026, 5)
    assert parse_period_name("Май  2026") == (2026, 5)


def test_parse_invalid_returns_none():
    """Нестандартные имена → None. Они получат ключ (0, 0) — попадут в baseline."""
    assert parse_period_name(None) is None
    assert parse_period_name("") is None
    assert parse_period_name("Начальный период") is None
    assert parse_period_name("Тестовый") is None
    assert parse_period_name("Май abc") is None
    assert parse_period_name("Майабырвалг 2026") is None
    assert parse_period_name("2026") is None
    assert parse_period_name("Май") is None


def test_parse_invalid_year_range():
    """Год вне 2000..2100 — отбраковываем как опечатку."""
    assert parse_period_name("Май 1999") is None
    assert parse_period_name("Май 2101") is None
    assert parse_period_name("Май 20226") is None  # опечатка из реальной жизни
    # А вот 2000 и 2100 — валидны (граничные).
    assert parse_period_name("Май 2000") == (2000, 5)
    assert parse_period_name("Май 2100") == (2100, 5)


def test_chron_key_baseline_sorts_first():
    """«Начальный период» должен сортироваться раньше любого реального — он baseline."""
    assert period_chron_key("Начальный период") == (0, 0)
    assert period_chron_key("Январь 2025") > period_chron_key("Начальный период")
    assert period_chron_key("Май 2026") > period_chron_key("Январь 2026")


def test_chronological_sort_full_year():
    """12 месяцев одного года — естественный порядок."""
    months = [
        "Декабрь 2026", "Январь 2026", "Июнь 2026", "Март 2026",
        "Февраль 2026", "Апрель 2026", "Май 2026", "Июль 2026",
        "Август 2026", "Сентябрь 2026", "Октябрь 2026", "Ноябрь 2026",
    ]
    sorted_months = sorted(months, key=period_chron_key)
    expected = [
        "Январь 2026", "Февраль 2026", "Март 2026", "Апрель 2026",
        "Май 2026", "Июнь 2026", "Июль 2026", "Август 2026",
        "Сентябрь 2026", "Октябрь 2026", "Ноябрь 2026", "Декабрь 2026",
    ]
    assert sorted_months == expected


def test_chronological_sort_year_boundary():
    """Декабрь предыдущего года < январь следующего."""
    items = ["Январь 2027", "Декабрь 2026", "Декабрь 2025"]
    sorted_items = sorted(items, key=period_chron_key)
    assert sorted_items == ["Декабрь 2025", "Декабрь 2026", "Январь 2027"]


def test_chronological_sort_includes_baseline():
    """Реальный сценарий: baseline + несколько месяцев в перемешку."""
    items = ["Май 2026", "Начальный период", "Февраль 2026", "Апрель 2026"]
    sorted_items = sorted(items, key=period_chron_key)
    # Baseline первый, затем хронологически
    assert sorted_items == [
        "Начальный период",
        "Февраль 2026",
        "Апрель 2026",
        "Май 2026",
    ]


def test_sorokin_scenario():
    """Регрессионный тест: исторический инцидент с Сорокиным С.А.

    В БД были периоды с id: 1 (Начальный), 2 (Апрель), 88 (Май), 90 (Февраль).
    Сортировка по `period.id DESC` давала [90, 88, 2, 1] = [Февраль, Май,
    Апрель, Начальный] — Февральская подача отображалась ВЫШЕ майской,
    дельта мая считалась относительно февраля.

    Правильная хронология: Май → Апрель → Февраль → Начальный.
    """
    period_names = ["Февраль 2026", "Май 2026", "Апрель 2026", "Начальный период"]
    sorted_desc = sorted(period_names, key=period_chron_key, reverse=True)
    assert sorted_desc == [
        "Май 2026",
        "Апрель 2026",
        "Февраль 2026",
        "Начальный период",
    ]
