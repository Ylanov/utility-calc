# tests/test_pick_prev.py
"""pick_prev_pair — ЕДИНЫЙ канонический выбор «прошлого показания».

Свёл 5 реализаций (аудит 2026-07-14). Ключевой класс багов: ретроактивный
период (создан позже → period_id БОЛЬШЕ, месяц РАНЬШЕ) — выбор по period_id
брал не тот prev, суммы на разных путях расходились.
"""
from datetime import datetime

from app.modules.utility.models import MeterReading
from app.modules.utility.services.reading_calculator import pick_prev_pair


def _r(flags=None, created=None, _c=[0]):
    _c[0] += 1
    return MeterReading(
        anomaly_flags=flags,
        created_at=created or datetime(2026, 1, 1, 0, 0, _c[0] % 60, _c[0] // 60),
    )


def test_simple_chain():
    apr, may = _r(), _r()
    prev, prev_any, earlier = pick_prev_pair(
        [(apr, "Апрель 2026"), (may, "Май 2026")], "Июнь 2026")
    assert prev is may and prev_any is may
    assert earlier == [may, apr]


def test_backdated_period_uses_month_not_id():
    # Апрель создан ПОЗЖЕ мая (ретроактивно, id больше) — prev для мая
    # всё равно апрель... а prev для ИЮНЯ — май, а не «свежесозданный» апрель.
    may = _r(created=datetime(2026, 6, 1))
    apr = _r(created=datetime(2026, 7, 10))  # создан последним!
    prev, _, _ = pick_prev_pair([(may, "Май 2026"), (apr, "Апрель 2026")], "Июнь 2026")
    assert prev is may
    prev2, _, _ = pick_prev_pair([(may, "Май 2026"), (apr, "Апрель 2026")], "Май 2026")
    assert prev2 is apr


def test_same_period_duplicate_not_prev():
    # Дубль того же месяца (инцидент Мороз) prev'ом не становится.
    may, june_dup = _r(), _r()
    prev, _, earlier = pick_prev_pair(
        [(may, "Май 2026"), (june_dup, "Июнь 2026")], "Июнь 2026")
    assert prev is may
    assert june_dup not in earlier


def test_meter_replacement_of_target_period_wins():
    # Замена счётчика в ТЕКУЩЕМ месяце — baseline нового прибора приоритетнее
    # прошлого месяца; METER_CLOSED (финал старого) — не meaningful.
    may = _r()
    closed = _r(flags="METER_CLOSED")
    repl = _r(flags="METER_REPLACEMENT")
    prev, _, _ = pick_prev_pair(
        [(may, "Май 2026"), (closed, "Июнь 2026"), (repl, "Июнь 2026")], "Июнь 2026")
    assert prev is repl


def test_synthetic_prev_skipped_but_prev_any_sees_it():
    # AUTO_NORM не годится как prev (дельта от синтетики), но synth-детекция
    # (prev_any) должна его видеть.
    apr = _r()
    may_auto = _r(flags="AUTO_NORM")
    prev, prev_any, _ = pick_prev_pair(
        [(apr, "Апрель 2026"), (may_auto, "Май 2026")], "Июнь 2026")
    assert prev is apr
    assert prev_any is may_auto


def test_no_history_is_baseline():
    prev, prev_any, earlier = pick_prev_pair([], "Июнь 2026")
    assert prev is None and prev_any is None and earlier == []


def test_initial_period_sorts_before_everything():
    ini = _r(flags="INITIAL_SETUP")
    prev, _, _ = pick_prev_pair([(ini, "Начальный период")], "Январь 2026")
    assert prev is ini
