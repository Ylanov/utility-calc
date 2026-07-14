# tests/test_onec_publish_guard.py
"""Константы предохранителя авто-выгрузки 1С.

Сам publish_onec_debts требует БД (интеграционный), но пороги — контракт:
guard должен ловить «сбор обнулил бы массу ненулевых долгов» (класс бага
US-формата: 98% долгов в ноль) и НЕ срабатывать на первом прогоне.
"""
from app.modules.utility.services.onec_publish import (
    GUARD_MIN_PREV, GUARD_ZERO_FRACTION,
)


def test_guard_thresholds_contract():
    # ≥50% обнулений при базе ≥20 ненулевых — сработает.
    assert 0 < GUARD_ZERO_FRACTION <= 0.5
    assert GUARD_MIN_PREV >= 10


def test_guard_math_examples():
    # Реальный баг: 98 из 100 ненулевых обнулились бы → трип.
    assert 98 / 100 >= GUARD_ZERO_FRACTION
    # Первый прогон: 0 ненулевых долгов — база меньше минимума, трипа нет.
    assert 0 < GUARD_MIN_PREV
    # Лёгкая ротация должников (5 из 100 закрыли долг) — трипа нет.
    assert 5 / 100 < GUARD_ZERO_FRACTION
