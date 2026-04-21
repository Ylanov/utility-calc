"""finance_analyzer.py — анализ финансовых аномалий по периодам.

Не путать с anomaly_detector.py — тот работает на уровне ОДНОЙ подачи показаний
(SPIKE_HOT, COPY_NEIGHBOR и т.д.). Этот — на уровне квитанций ЗА ПЕРИОД:
долг растёт, счёт скакнул, нулевая квитанция, переплата подозрительна и т.д.

Все правила управляются из «Центра анализа» (analyzer_settings, категория
'finance'). Self-learning через anomaly_dismissals тоже работает —
если флаг помечен dismissed для жильца, он не выставляется.

Вызывается из admin_reports.py при генерации финансовой сводки v2.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.modules.utility.services.analyzer_config import config, dismissals


# Severity для UI: соответствует Bootstrap-style цветам.
FLAG_SEVERITY = {
    "DEBT_GROWING": "high",
    "BILL_SPIKE": "medium",
    "BILL_DROP": "medium",
    "ZERO_BILL": "high",
    "OVERPAY_SUSPECT": "low",
    "HIGH_BILL_PER_PERSON": "medium",
    "MISSING_RECEIPT": "high",
    "WRONG_BILLING_MODE": "medium",
}


def _D(v) -> Decimal:
    if v is None:
        return Decimal("0")
    return Decimal(str(v))


def analyze_finance(
    *,
    user_id: Optional[int],
    residents_count: int,
    current_total_cost: Optional[Decimal],
    current_debt: Decimal,
    current_overpayment: Decimal,
    prev_costs: list[Decimal],          # суммы прошлых N периодов (старые → новые)
    prev_debts: list[Decimal],          # долги прошлых N периодов
    has_reading: bool,                  # есть ли MeterReading в текущем периоде вообще
    resident_type: str = "family",      # 'family' | 'single'
    billing_mode: str = "by_meter",     # 'by_meter' | 'per_capita'
) -> tuple[list[str], int]:
    """Возвращает (список финансовых флагов, suggested risk-level 0..100).

    Все суммы в рублях (Decimal). Использовать после агрегации по периоду.
    """
    flags: list[str] = []
    score = 0
    rc = max(int(residents_count or 1), 1)

    # ---------- MISSING_RECEIPT ----------
    if config.is_rule_enabled("finance.missing_receipt") and not has_reading:
        flags.append("MISSING_RECEIPT")
        score += 40
        # Если нет показания — остальные правила бессмысленны.
        return _filter_dismissed(flags, score, user_id)

    cur = _D(current_total_cost)

    # ---------- ZERO_BILL ----------
    if config.is_rule_enabled("finance.zero_bill") and cur == 0 and prev_costs:
        # Нулевая квитанция при том что в истории были начисления.
        nonzero_history = [c for c in prev_costs if c > 0]
        if nonzero_history:
            avg = sum(nonzero_history) / len(nonzero_history)
            if avg > Decimal("100"):
                flags.append("ZERO_BILL")
                score += 35

    # ---------- BILL_SPIKE / BILL_DROP ----------
    if prev_costs:
        last = prev_costs[-1]
        if last > 0 and cur > 0:
            change_pct = float((cur - last) / last * 100)
            if (
                config.is_rule_enabled("finance.bill_spike")
                and change_pct >= config.get_int("finance.bill_spike.threshold_pct", 50)
            ):
                flags.append("BILL_SPIKE")
                score += 25
            elif (
                config.is_rule_enabled("finance.bill_drop")
                and change_pct <= -config.get_int("finance.bill_drop.threshold_pct", 50)
            ):
                flags.append("BILL_DROP")
                score += 20

    # ---------- DEBT_GROWING ----------
    # Долг строго растёт 3+ периода подряд (включая текущий).
    if config.is_rule_enabled("finance.debt_growing"):
        chain = list(prev_debts) + [current_debt]
        if len(chain) >= 3:
            tail = chain[-3:]
            if tail[0] < tail[1] < tail[2] and tail[2] > Decimal("500"):
                flags.append("DEBT_GROWING")
                score += 30

    # ---------- OVERPAY_SUSPECT ----------
    if (
        config.is_rule_enabled("finance.overpay_suspect")
        and current_overpayment > config.get_int("finance.overpay_suspect.threshold_rub", 10000)
    ):
        flags.append("OVERPAY_SUSPECT")
        score += 10

    # ---------- HIGH_BILL_PER_PERSON ----------
    if config.is_rule_enabled("finance.high_bill_per_person") and cur > 0:
        per_person = cur / Decimal(str(rc))
        thr = Decimal(str(config.get_int("finance.high_bill_per_person.threshold_rub", 8000)))
        if per_person > thr:
            flags.append("HIGH_BILL_PER_PERSON")
            score += 20

    # ---------- WRONG_BILLING_MODE ----------
    # Несоответствие типа жильца и режима оплаты — показатель неконсистентных
    # данных. Например, single (холостяк) всегда должен быть на per_capita,
    # а family с counters — на by_meter. Если не так — кто-то ввёл руками.
    if config.is_rule_enabled("finance.wrong_billing_mode"):
        expected = "per_capita" if resident_type == "single" else "by_meter"
        if billing_mode != expected:
            flags.append("WRONG_BILLING_MODE")
            score += 15

    return _filter_dismissed(flags, min(score, 100), user_id)


def _filter_dismissed(
    flags: list[str], score: int, user_id: Optional[int]
) -> tuple[list[str], int]:
    """Снимаем флаги которые админ пометил как «не аномалия для этого жильца»."""
    if not flags:
        return [], 0
    kept: list[str] = []
    dropped = 0
    for f in flags:
        if dismissals.is_dismissed(user_id, f):
            dropped += 1
        else:
            kept.append(f)
    if not kept:
        return [], 0
    # Грубая корректировка score — точная не нужна, главное чтобы dismissed
    # понижали уровень риска пропорционально.
    new_score = max(0, score - dropped * 15)
    return kept, new_score


# Каталог финансовых флагов для UI справки.
FLAG_CATALOG = [
    {"code": "DEBT_GROWING", "severity": "high",
     "title": "Долг растёт",
     "desc": "Сумма задолженности увеличивается 3+ месяца подряд."},
    {"code": "BILL_SPIKE", "severity": "medium",
     "title": "Резкий рост счёта",
     "desc": "Сумма квитанции значительно превышает предыдущую."},
    {"code": "BILL_DROP", "severity": "medium",
     "title": "Резкое падение счёта",
     "desc": "Сумма квитанции упала — возможно жилец не подал показания."},
    {"code": "ZERO_BILL", "severity": "high",
     "title": "Нулевая квитанция",
     "desc": "Сумма = 0 при том что в истории были начисления."},
    {"code": "OVERPAY_SUSPECT", "severity": "low",
     "title": "Подозрительная переплата",
     "desc": "Большая сумма переплаты — возможна ошибка импорта из 1С."},
    {"code": "HIGH_BILL_PER_PERSON", "severity": "medium",
     "title": "Большой счёт на 1 человека",
     "desc": "Начисление на одного проживающего превышает порог."},
    {"code": "MISSING_RECEIPT", "severity": "high",
     "title": "Нет квитанции за период",
     "desc": "У жильца не утверждено ни одного показания за этот период."},
    {"code": "WRONG_BILLING_MODE", "severity": "medium",
     "title": "Несоответствие типа жильца и режима оплаты",
     "desc": "Холостяк (single) должен быть на per_capita, семья (family) — by_meter."},
]
