"""Тесты разграничения «начислено по нормативу» vs «по показаниям» (бейдж
в приложении жильца) и ИНВАРИАНТА, на котором держится фикс порога импорта
из Google-таблиц: машинные AUTO_NORM не должны становиться порогом «не ниже
предыдущего» (иначе реальная подача ниже норматива валится «счётчик упал»).
"""
from app.modules.utility.services.anomaly_flags import (
    is_estimated_charge,
    ESTIMATED_CHARGE_FLAGS,
)
from app.modules.utility.services.reading_calculator import (
    PREV_SKIP_FLAGS,
    is_meaningful_prev,
)


class _R:
    """Минимальная заглушка MeterReading для is_meaningful_prev."""

    def __init__(self, flags):
        self.anomaly_flags = flags


def test_estimated_charge_true_for_norm():
    assert is_estimated_charge("AUTO_NORM") is True
    assert is_estimated_charge("AUTO_NORM_SANCTION") is True
    assert is_estimated_charge("auto_norm") is True  # регистронезависимо
    assert is_estimated_charge("PENDING,AUTO_NORM") is True  # среди других токенов


def test_estimated_charge_false_for_real_submission():
    assert is_estimated_charge("PENDING") is False
    assert is_estimated_charge("BASELINE") is False
    assert is_estimated_charge("PENDING,SPIKE_HOT") is False
    assert is_estimated_charge("GSHEETS_AUTO") is False  # реальная подача из таблиц
    assert is_estimated_charge(None) is False
    assert is_estimated_charge("") is False


def test_auto_generated_not_estimated_charge():
    # AUTO_GENERATED — нулевой baseline, начисления нет → не «по нормативу».
    assert is_estimated_charge("AUTO_GENERATED") is False


def test_norm_flags_are_prev_skip():
    # Инвариант фикса gsheets_sync.prev_subq: каждый машинный charge-флаг
    # исключается из prev, иначе оценка стала бы порогом и реальная меньшая
    # подача валилась бы «счётчик упал».
    for flag in ESTIMATED_CHARGE_FLAGS:
        assert flag in PREV_SKIP_FLAGS, f"{flag} должен быть в PREV_SKIP_FLAGS"
        assert is_meaningful_prev(_R(flag)) is False


def test_real_reading_is_meaningful_prev():
    assert is_meaningful_prev(_R("PENDING")) is True
    assert is_meaningful_prev(_R("BASELINE")) is True
    assert is_meaningful_prev(_R(None)) is True
