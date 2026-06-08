"""Тесты норматив-объёмов и санкции ×3 (_growing_norm_volumes).

Ключевой инвариант (2026-06-08): квартиры/дома БЕЗ счётчиков не должны
попадать под санкцию ×3 — они не виноваты, что подавать нечего. Норматив
гейтится по has_*_meter комнаты, санкция применяется только если есть хоть
один начисляемый мётрируемый ресурс.
"""
from decimal import Decimal as D

from app.modules.utility.services.billing import _growing_norm_volumes


class _T:
    """Заглушка тарифа."""

    def __init__(self, coef=3, hw=3, cw=4, el=100,
                 ch_hw=True, ch_cw=True, ch_el=True):
        self.norm_coefficient = coef
        self.hw_norm_per_capita = hw
        self.cw_norm_per_capita = cw
        self.el_norm_per_capita = el
        self.charge_hot_water = ch_hw
        self.charge_cold_water = ch_cw
        self.charge_electricity = ch_el


class _R:
    """Заглушка комнаты (наличие счётчиков)."""

    def __init__(self, hw=True, cw=True, el=True):
        self.has_hw_meter = hw
        self.has_cw_meter = cw
        self.has_el_meter = el


def test_metered_room_sanction_at_3():
    vh, vc, ve, coef = _growing_norm_volumes(_T(), D(1), 3, room=_R())
    assert coef == D(3)
    assert vh == D(9) and vc == D(12) and ve == D(300)  # норматив × 3


def test_metered_room_no_sanction_below_3():
    vh, vc, ve, coef = _growing_norm_volumes(_T(), D(1), 2, room=_R())
    assert coef == D(1)
    assert vh == D(3) and vc == D(4) and ve == D(100)  # × 1


def test_no_meter_room_never_sanctioned():
    # Все счётчики отсутствуют → санкции нет даже при огромном miss_count,
    # норматив-объём = 0 (показание не раздувается).
    r = _R(hw=False, cw=False, el=False)
    vh, vc, ve, coef = _growing_norm_volumes(_T(), D(1), 99, room=r)
    assert coef == D(1)
    assert vh == D(0) and vc == D(0) and ve == D(0)


def test_partial_meter_only_metered_resource_normed():
    # Есть только электросчётчик → нормируется/эскалирует только электричество.
    r = _R(hw=False, cw=False, el=True)
    vh, vc, ve, coef = _growing_norm_volumes(_T(), D(1), 3, room=r)
    assert coef == D(3)
    assert vh == D(0) and vc == D(0)
    assert ve == D(300)


def test_house_charge_off_no_norm():
    # Дом: charge_*=False → норматив 0, без санкции (хотя счётчики «есть»).
    t = _T(ch_hw=False, ch_cw=False, ch_el=False)
    vh, vc, ve, coef = _growing_norm_volumes(t, D(1), 5, room=_R())
    assert coef == D(1)
    assert (vh, vc, ve) == (D(0), D(0), D(0))


def test_no_room_preserves_legacy():
    # room=None → счётчики считаются присутствующими (прежнее поведение).
    vh, vc, ve, coef = _growing_norm_volumes(_T(), D(1), 3, room=None)
    assert coef == D(3)
    assert vh == D(9)
