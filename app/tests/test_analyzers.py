"""Тесты для анализаторов: anomaly_detector + finance_analyzer.

Покрытие минимально, но защищает критическую логику scoring и self-learning
от регрессий — особенно после рефактора may 2026 (точный _flag_score
вместо «-15/-20 за каждый dismissed»).

Recalc-drift и telemetry тестируются интеграционно через endpoint и
требуют БД, поэтому тут не покрыты — добавятся отдельным контуром
performance-tests.
"""
from decimal import Decimal

from app.modules.utility.services.anomaly_detector import (
    _flag_score,
    analyze_resource,
    check_reading_for_anomalies_v2,
)
from app.modules.utility.services.cohort_analyzer import (
    ALLOWED_METRICS,
    _area_bucket,
    _family_bucket,
    _stats,
)
from app.modules.utility.services.finance_analyzer import (
    _FLAG_SCORES,
    analyze_finance,
)


# ============================================================
# anomaly_detector: _flag_score
# ============================================================

def test_flag_score_known_critical():
    """NEGATIVE_X — самый тяжёлый флаг, 100 баллов."""
    assert _flag_score("NEGATIVE_HOT") == 100
    assert _flag_score("NEGATIVE_COLD") == 100
    assert _flag_score("NEGATIVE_ELECT") == 100


def test_flag_score_spike_family():
    """SPIKE_*, DROP_AFTER_SPIKE_* — 40."""
    assert _flag_score("SPIKE_HOT") == 40
    assert _flag_score("DROP_AFTER_SPIKE_COLD") == 40


def test_flag_score_v3_rules():
    """Правила v3 — каждое со своим score."""
    assert _flag_score("HOT_GT_COLD") == 30
    assert _flag_score("COPY_NEIGHBOR") == 35
    assert _flag_score("COPY_NEIGHBOR_PARTIAL") == 15
    assert _flag_score("GAP_RECOVERY") == 25
    assert _flag_score("COMBO_SUSPICIOUS") == 25


def test_flag_score_unknown_fallback():
    """Неизвестный флаг получает fallback 20 баллов — не падает."""
    assert _flag_score("FUTURE_RULE_X") == 20
    assert _flag_score("WHATEVER") == 20


def test_flag_score_high_disambiguation():
    """HIGH_VS_PEERS_X должен возвращать 20, а HIGH_X (просто) тоже 20,
    HIGH_PER_PERSON_X — 25 (особый случай)."""
    assert _flag_score("HIGH_HOT") == 20  # soft spike
    assert _flag_score("HIGH_VS_PEERS_HOT") == 20
    assert _flag_score("HIGH_PER_PERSON_COLD") == 25
    assert _flag_score("HIGH_PER_PERSON_ELECT") == 25


# ============================================================
# anomaly_detector: analyze_resource
# ============================================================

def test_analyze_resource_no_history():
    """Без истории — никаких флагов и score=0."""
    flags, score = analyze_resource(Decimal("5"), [], "HOT")
    assert flags == []
    assert score == 0


def test_analyze_resource_zero_with_history():
    """ZERO_X срабатывает когда дельта 0 при ненулевой истории."""
    flags, score = analyze_resource(
        current_delta=Decimal("0"),
        hist_deltas=[Decimal("3"), Decimal("4"), Decimal("3.5")],
        name="HOT",
    )
    assert "ZERO_HOT" in flags
    assert score >= 25


def test_analyze_resource_frozen():
    """4 нуля подряд (3 в истории + текущий) — FROZEN."""
    flags, score = analyze_resource(
        current_delta=Decimal("0"),
        hist_deltas=[Decimal("0"), Decimal("0"), Decimal("0")],
        name="COLD",
    )
    # FROZEN ставится если ВСЕ history нули И текущая 0.
    # ZERO ставится только если med > 1, тут med=0, не сработает.
    assert "FROZEN_COLD" in flags


def test_analyze_resource_normal_no_flags():
    """Дельта в пределах median — никаких флагов."""
    flags, score = analyze_resource(
        current_delta=Decimal("4.5"),
        hist_deltas=[Decimal("4"), Decimal("5"), Decimal("4.5"), Decimal("5"), Decimal("4")],
        name="HOT",
    )
    assert flags == []
    assert score == 0


# ============================================================
# anomaly_detector: end-to-end (минимум)
# ============================================================

class _FakeReading:
    def __init__(self, hot=0, cold=0, elect=0, user_id=1, created_at=None):
        self.hot_water = Decimal(str(hot))
        self.cold_water = Decimal(str(cold))
        self.electricity = Decimal(str(elect))
        self.user_id = user_id
        self.created_at = created_at


class _FakeUser:
    def __init__(self, residents=2, uid=1,
                 has_hw_meter=True, has_cw_meter=True, has_el_meter=True):
        self.id = uid
        self.residents_count = residents
        # Конфигурация счётчиков (meters_001_per_user_config). По умолчанию
        # True — анализатор флагит всё как раньше; False — пропускает ресурс.
        self.has_hw_meter = has_hw_meter
        self.has_cw_meter = has_cw_meter
        self.has_el_meter = has_el_meter


def test_check_reading_no_history_returns_none():
    """Нет истории → анализатор молчит."""
    cur = _FakeReading(hot=10, cold=20, elect=100)
    flags, score = check_reading_for_anomalies_v2(cur, history=[])
    assert flags is None
    assert score == 0


def test_check_reading_negative_delta_critical():
    """Если новые показания МЕНЬШЕ — NEGATIVE_X с score 100."""
    history = [
        _FakeReading(hot=100, cold=200, elect=500),
        _FakeReading(hot=95, cold=190, elect=480),
    ]
    cur = _FakeReading(hot=90, cold=180, elect=470)  # отрицательная дельта
    flags, score = check_reading_for_anomalies_v2(cur, history=history)
    assert flags is not None
    assert "NEGATIVE_HOT" in flags
    # score достигает 100 (cap)
    assert score == 100


# ============================================================
# anomaly_detector: meters_001_per_user_config — отсутствующие счётчики
# ============================================================

def test_analyze_resource_skips_when_meter_absent():
    """analyze_resource(meter_present=False) — никаких флагов, даже при ZERO/FROZEN."""
    flags, score = analyze_resource(
        current_delta=Decimal("0"),
        hist_deltas=[Decimal("3"), Decimal("4"), Decimal("3.5")],
        name="HOT",
        meter_present=False,
    )
    assert flags == []
    assert score == 0


def test_check_reading_skips_negative_for_absent_meter():
    """Если has_el_meter=False — NEGATIVE_ELECT не выставляется даже при отрицательной дельте.
    Раньше анализатор флагил «дельта = 0 - 500 = -500», теперь — игнорирует ресурс целиком.
    """
    history = [
        _FakeReading(hot=100, cold=200, elect=500),
        _FakeReading(hot=95, cold=190, elect=480),
    ]
    # Жилец сдал hot/cold нормально, по элекстричеству счётчика нет → подаёт 0
    cur = _FakeReading(hot=105, cold=205, elect=0)
    user = _FakeUser(has_el_meter=False)
    flags, score = check_reading_for_anomalies_v2(cur, history=history, user=user)
    # NEGATIVE_ELECT не должен быть в флагах — анализатор пропустил ресурс
    assert flags is None or "NEGATIVE_ELECT" not in (flags or "")
    # NEGATIVE_HOT/COLD тоже не должно быть — там дельта положительная
    assert flags is None or "NEGATIVE_HOT" not in (flags or "")
    assert flags is None or "NEGATIVE_COLD" not in (flags or "")


def test_check_reading_meter_present_default_for_none_user():
    """Если user=None — все счётчики считаются присутствующими (старое поведение).
    Это гарантия что вызовы без user не падают и работают как до меньшетвения.
    """
    history = [
        _FakeReading(hot=100, cold=200, elect=500),
        _FakeReading(hot=95, cold=190, elect=480),
    ]
    cur = _FakeReading(hot=90, cold=205, elect=510)  # hot -- отрицательная
    flags, score = check_reading_for_anomalies_v2(cur, history=history, user=None)
    # Без user — анализатор работает как раньше, NEGATIVE_HOT должен выйти
    assert flags is not None
    assert "NEGATIVE_HOT" in flags


# ============================================================
# finance_analyzer: _FLAG_SCORES
# ============================================================

def test_finance_flag_scores_complete():
    """Все 8 финансовых флагов имеют score в _FLAG_SCORES."""
    expected = {
        "MISSING_RECEIPT", "ZERO_BILL", "DEBT_GROWING", "BILL_SPIKE",
        "BILL_DROP", "HIGH_BILL_PER_PERSON", "WRONG_BILLING_MODE", "OVERPAY_SUSPECT",
    }
    assert expected.issubset(set(_FLAG_SCORES.keys()))


def test_finance_flag_scores_ordering():
    """Severity отражена в score: high > medium > low."""
    # MISSING_RECEIPT (high) должен быть >= BILL_SPIKE (medium)
    assert _FLAG_SCORES["MISSING_RECEIPT"] >= _FLAG_SCORES["BILL_SPIKE"]
    # BILL_SPIKE (medium) > OVERPAY_SUSPECT (low)
    assert _FLAG_SCORES["BILL_SPIKE"] > _FLAG_SCORES["OVERPAY_SUSPECT"]


# ============================================================
# finance_analyzer: end-to-end
# ============================================================

def test_finance_missing_receipt_only_flag():
    """has_reading=False → должен быть только MISSING_RECEIPT, остальные правила пропускаются."""
    flags, score = analyze_finance(
        user_id=1, residents_count=2,
        current_total_cost=None, current_debt=Decimal("0"),
        current_overpayment=Decimal("0"),
        prev_costs=[Decimal("5000"), Decimal("4500")],
        prev_debts=[Decimal("0"), Decimal("0")],
        has_reading=False,
    )
    # missing_receipt отрабатывает раньше всех остальных правил
    assert "MISSING_RECEIPT" in flags
    # ZERO_BILL не выставляется — мы вышли по early-return на missing
    assert "ZERO_BILL" not in flags


def test_finance_normal_no_flags():
    """Нормальный счёт похожий на средний из истории — никаких флагов."""
    flags, score = analyze_finance(
        user_id=1, residents_count=2,
        current_total_cost=Decimal("5200"),
        current_debt=Decimal("0"),
        current_overpayment=Decimal("0"),
        prev_costs=[Decimal("4900"), Decimal("5000"), Decimal("5100")],
        prev_debts=[Decimal("0"), Decimal("0"), Decimal("0")],
        has_reading=True,
    )
    assert flags == []
    assert score == 0


def test_finance_bill_spike():
    """Счёт вырос больше 50% — BILL_SPIKE."""
    flags, score = analyze_finance(
        user_id=1, residents_count=2,
        current_total_cost=Decimal("9000"),  # 80% больше предыдущего
        current_debt=Decimal("0"),
        current_overpayment=Decimal("0"),
        prev_costs=[Decimal("5000")],
        prev_debts=[Decimal("0")],
        has_reading=True,
    )
    assert "BILL_SPIKE" in flags
    assert score >= 25


def test_finance_zero_bill_with_history():
    """total=0 при ненулевой истории — ZERO_BILL."""
    flags, score = analyze_finance(
        user_id=1, residents_count=2,
        current_total_cost=Decimal("0"),
        current_debt=Decimal("0"),
        current_overpayment=Decimal("0"),
        prev_costs=[Decimal("4000"), Decimal("4500"), Decimal("5000")],
        prev_debts=[Decimal("0"), Decimal("0"), Decimal("0")],
        has_reading=True,
    )
    assert "ZERO_BILL" in flags


def test_finance_debt_growing_three_periods():
    """Долг растёт 3 периода подряд — DEBT_GROWING."""
    flags, score = analyze_finance(
        user_id=1, residents_count=2,
        current_total_cost=Decimal("5000"),
        current_debt=Decimal("3000"),  # текущий
        current_overpayment=Decimal("0"),
        prev_costs=[Decimal("5000"), Decimal("5000")],
        prev_debts=[Decimal("1000"), Decimal("2000")],  # 1k → 2k → 3k
        has_reading=True,
    )
    assert "DEBT_GROWING" in flags


def test_finance_single_by_meter_is_ok():
    """Холостяк (single) на by_meter — это КОРРЕКТНО (2026-06-17): современная
    модель — общие счётчики квартиры + делёж по room.is_singles_apartment,
    per_capita — legacy. Раньше ошибочно флажили всех холостяков
    «Несоответствие типа жильца» — теперь НЕ флажим."""
    flags, score = analyze_finance(
        user_id=1, residents_count=1,
        current_total_cost=Decimal("3000"),
        current_debt=Decimal("0"),
        current_overpayment=Decimal("0"),
        prev_costs=[Decimal("3000")],
        prev_debts=[Decimal("0")],
        has_reading=True,
        resident_type="single",
        billing_mode="by_meter",
    )
    assert "WRONG_BILLING_MODE" not in flags


def test_finance_wrong_billing_mode_per_capita_flagged():
    """per_capita — legacy-режим, ручной ввод → флажим как подозрительный."""
    flags, score = analyze_finance(
        user_id=1, residents_count=1,
        current_total_cost=Decimal("3000"),
        current_debt=Decimal("0"),
        current_overpayment=Decimal("0"),
        prev_costs=[Decimal("3000")],
        prev_debts=[Decimal("0")],
        has_reading=True,
        resident_type="single",
        billing_mode="per_capita",
    )
    assert "WRONG_BILLING_MODE" in flags


def test_finance_high_bill_per_person():
    """Счёт > 8000 ₽ на человека — HIGH_BILL_PER_PERSON."""
    flags, score = analyze_finance(
        user_id=1, residents_count=1,
        current_total_cost=Decimal("9000"),
        current_debt=Decimal("0"),
        current_overpayment=Decimal("0"),
        prev_costs=[Decimal("8500")],
        prev_debts=[Decimal("0")],
        has_reading=True,
    )
    assert "HIGH_BILL_PER_PERSON" in flags


# ============================================================
# cohort_analyzer: pure helpers (без БД)
# ============================================================

def test_family_bucket_solo():
    assert _family_bucket(0) == "1 (одиночка)"
    assert _family_bucket(1) == "1 (одиночка)"


def test_family_bucket_pair():
    assert _family_bucket(2) == "2 (пара)"


def test_family_bucket_family():
    assert _family_bucket(3) == "3-4 (семья)"
    assert _family_bucket(4) == "3-4 (семья)"


def test_family_bucket_large():
    assert _family_bucket(5) == "5+ (большая семья)"
    assert _family_bucket(10) == "5+ (большая семья)"


def test_area_bucket_no_quartiles():
    """Если quartiles пустые — fallback на «—»."""
    assert _area_bucket(30.0, []) == "—"


def test_area_bucket_quartiles():
    quartiles = [20.0, 30.0, 40.0]
    assert "small" in _area_bucket(15.0, quartiles)
    assert "medium" in _area_bucket(25.0, quartiles)
    assert "large" in _area_bucket(35.0, quartiles)
    assert "xlarge" in _area_bucket(50.0, quartiles)


def test_stats_empty():
    """Пустой список — все None кроме count."""
    s = _stats([])
    assert s["count"] == 0
    assert s["median"] is None
    assert s["p95"] is None


def test_stats_single():
    """Одно значение — median=p95=max=min."""
    s = _stats([Decimal("100")])
    assert s["count"] == 1
    assert s["median"] == 100.0
    assert s["max"] == 100.0
    assert s["min"] == 100.0
    assert s["p95"] == 100.0


def test_stats_typical():
    """Несколько значений — корректный median/p95."""
    s = _stats([Decimal(x) for x in (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)])
    assert s["count"] == 10
    assert s["min"] == 10.0
    assert s["max"] == 100.0
    # median между 50 и 60 = 55
    assert s["median"] == 55.0
    # p95 = 95-й процентиль (индекс int(0.95 * 9) = 8 → значение 90)
    assert s["p95"] == 90.0


def test_allowed_metrics_complete():
    """Все 4 метрики анализатор должен поддерживать."""
    assert "total_cost" in ALLOWED_METRICS
    assert "hot_water" in ALLOWED_METRICS
    assert "cold_water" in ALLOWED_METRICS
    assert "electricity" in ALLOWED_METRICS
