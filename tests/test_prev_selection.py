# tests/test_prev_selection.py
"""is_meaningful_prev / PREV_SKIP_FLAGS — какие показания годятся как «прошлое».

Инциденты: дельта от синтетического AUTO_*-нуля (счёт 456 562 руб., Пегарьков),
«счётчик упал» от AUTO_AVG (Вастаев), METER_CLOSED как prev нового счётчика.
"""
from app.modules.utility.models import MeterReading
from app.modules.utility.services.reading_calculator import (
    PREV_SKIP_FLAGS, is_meaningful_prev,
)


def _r(flags):
    return MeterReading(anomaly_flags=flags)


def test_none_is_not_meaningful():
    assert not is_meaningful_prev(None)


def test_real_submissions_are_meaningful():
    for fl in (None, "", "BASELINE", "GSHEETS_AUTO_BASELINE", "INITIAL_SETUP",
               "COMBO_SUSPICIOUS", "ZERO_COLD", "METER_REPLACEMENT"):
        assert is_meaningful_prev(_r(fl)), fl


def test_synthetic_flags_are_skipped():
    for fl in PREV_SKIP_FLAGS:
        assert not is_meaningful_prev(_r(fl)), fl


def test_flag_inside_combined_string():
    # Флаги хранятся строкой через запятую — вхождение тоже дисквалифицирует.
    assert not is_meaningful_prev(_r("SPIKE_HOT,AUTO_NORM"))
    assert not is_meaningful_prev(_r("METER_CLOSED,MANUAL"))


def test_meter_replacement_vs_meter_closed():
    # Замена счётчика: METER_CLOSED (финал старого прибора) — НЕ prev,
    # METER_REPLACEMENT (baseline нового) — валидный prev.
    assert not is_meaningful_prev(_r("METER_CLOSED"))
    assert is_meaningful_prev(_r("METER_REPLACEMENT"))
