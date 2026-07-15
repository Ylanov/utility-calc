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


def test_month_period_name_roundtrip():
    # month_period_name даёт ровно тот формат, который парсит period_chron_key —
    # на этом держится автопогашение буфера GSheets (gsheets_supersede).
    from datetime import datetime
    from app.modules.utility.services.period_helpers import month_period_name

    assert month_period_name(datetime(2026, 6, 15)) == "Июнь 2026"
    assert month_period_name(datetime(2025, 12, 1)) == "Декабрь 2025"
    for m in range(1, 13):
        name = month_period_name(datetime(2026, m, 10))
        assert period_chron_key(name) == (2026, m)
