# tests/test_period_chronology.py
"""period_chron_key — биллинговая хронология по ИМЕНИ периода.

Инциденты «задним числом»: period.id отражает порядок создания, а не месяц
(ретроактивный «Февраль 2026» создан в мае → id больше майского). Все выборы
prev (ручной ввод, карточка жильца, квитанция) обязаны идти по chron_key.
"""
from app.modules.utility.services.period_helpers import period_chron_key


def test_months_ordered_within_year():
    names = ["Январь 2026", "Февраль 2026", "Июнь 2026", "Декабрь 2026"]
    keys = [period_chron_key(n) for n in names]
    assert keys == sorted(keys)
    assert keys[0] == (2026, 1) and keys[-1] == (2026, 12)


def test_year_dominates_month():
    assert period_chron_key("Декабрь 2025") < period_chron_key("Январь 2026")


def test_initial_period_sorts_first():
    # «Начальный период» и непарсимые имена = (0, 0) — baseline до истории.
    assert period_chron_key("Начальный период") == (0, 0)
    assert period_chron_key(None) == (0, 0)
    assert period_chron_key("тест") == (0, 0)
    assert period_chron_key("Начальный период") < period_chron_key("Январь 2020")


def test_backdated_period_scenario():
    # Ретроактивный апрель (создан позже мая, id больше) всё равно РАНЬШЕ мая.
    assert period_chron_key("Апрель 2026") < period_chron_key("Май 2026")
    assert period_chron_key("Май 2026") < period_chron_key("Июнь 2026")
