"""telemetry_analyzer.py — статистика подач для админа.

Что собирает по периоду:
  - Сколько reading'ов создано через каждый источник (gsheets/мобилка/admin/baseline)
  - % reading'ов с anomaly-флагами и средний risk score
  - Распределение reading'ов по дням периода (когда жильцы подают)
  - Кол-во жильцов без квитанции (missing receipt) vs всего жильцов
  - Среднее total_cost, медиана, p95 (для понимания «здоровья» расчётов)
  - Топ типов аномалий по количеству

Не путать с finance_analyzer (работает с одним жильцом) и
anomaly_detector (работает с одной подачей). Telemetry — это сводка
по ВСЕМУ периоду, для дашборда «Центра анализа».

Используется в /api/admin/analyzer/telemetry?period_id=N.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Optional

from sqlalchemy import func, select

from app.modules.utility.models import (
    BillingPeriod,
    MeterReading,
    Room,
    User,
)

logger = logging.getLogger(__name__)


def _classify_source(anomaly_flags: Optional[str]) -> str:
    """Маппинг anomaly_flags первого токена → канал подачи."""
    if not anomaly_flags:
        return "mobile_or_manual"  # без флагов = ручная/мобильная подача
    f = anomaly_flags.split(",")[0].strip().upper()
    if "GSHEETS_AUTO_BASELINE" in f:
        return "gsheets_auto_baseline"
    if "GSHEETS_AUTO" in f:
        return "gsheets_auto"
    if "GSHEETS_IMPORT" in f:
        return "gsheets_manual"
    if "BASELINE" in f:
        return "baseline"
    if "ONE_TIME_CHARGE" in f:
        return "one_time"
    if "AUTO_GENERATED" in f:
        return "auto_generated"
    if "DATA_OVERFLOW_RESET" in f:
        return "cleanup_reset"
    return "mobile_or_manual"


async def collect_period_telemetry(db, period_id: int) -> dict:
    """Собирает телеметрию по одному периоду.

    Возвращает dict с разделами:
      meta, by_source, anomalies, totals, day_distribution, missing.
    """
    period = await db.get(BillingPeriod, period_id)
    if not period:
        return {"fatal": f"period_id={period_id} не найден"}

    # 1. Все approved-readings периода — основная выборка
    readings_q = await db.execute(
        select(MeterReading).where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True),
        )
    )
    readings = list(readings_q.scalars().all())

    # 2. Кол-во активных жильцов с комнатой (для missing-калькуляции)
    eligible_q = await db.execute(
        select(func.count(User.id)).where(
            User.role == "user",
            User.is_deleted.is_(False),
            User.room_id.is_not(None),
        )
    )
    eligible_count = eligible_q.scalar_one() or 0

    # 3. By source
    by_source: Counter = Counter()
    for r in readings:
        by_source[_classify_source(r.anomaly_flags)] += 1

    # 4. Anomaly stats — кто-сколько-каких флагов
    flagged = 0
    flag_counter: Counter = Counter()
    scores: list[int] = []
    for r in readings:
        if r.anomaly_flags:
            # Не считаем чисто «source-маркеры» как аномалии — они
            # технические (GSHEETS_AUTO, BASELINE и т.п.). Аномалии
            # = SPIKE_*, FLAT_*, ZERO_*, COPY_NEIGHBOR и т.д.
            tokens = [t.strip() for t in r.anomaly_flags.split(",") if t.strip()]
            real = [
                t for t in tokens
                if not (
                    t.startswith("GSHEETS_") or t == "BASELINE"
                    or t.startswith("ONE_TIME") or t == "AUTO_GENERATED"
                    or t == "DATA_OVERFLOW_RESET"
                )
            ]
            if real:
                flagged += 1
                for t in real:
                    flag_counter[t] += 1
        if r.anomaly_score is not None:
            scores.append(int(r.anomaly_score))

    avg_score = (sum(scores) / len(scores)) if scores else 0.0
    max_score = max(scores) if scores else 0

    # 5. Totals stats
    totals = [Decimal(str(r.total_cost or 0)) for r in readings]
    nonzero_totals = [t for t in totals if t > 0]

    def _q(values, fn):
        if not values:
            return None
        if fn == "med":
            return float(median(values))
        if fn == "max":
            return float(max(values))
        if fn == "min":
            return float(min(values))
        if fn == "avg":
            return float(sum(values) / len(values))
        if fn == "p95":
            xs = sorted(values)
            return float(xs[int(0.95 * (len(xs) - 1))])
        return None

    totals_summary = {
        "count": len(totals),
        "count_nonzero": len(nonzero_totals),
        "min": _q(nonzero_totals, "min"),
        "median": _q(nonzero_totals, "med"),
        "avg": _q(nonzero_totals, "avg"),
        "p95": _q(nonzero_totals, "p95"),
        "max": _q(nonzero_totals, "max"),
        "sum": float(sum(totals)),
    }

    # 6. Распределение подач по дням периода — когда жильцы подают.
    # Берём день месяца от created_at. Это даёт картинку «25 числа все
    # подают подряд» или «равномерно весь период».
    day_distribution: Counter = Counter()
    for r in readings:
        if r.created_at:
            day_distribution[r.created_at.day] += 1
    # Превращаем в массив 1..31 для UI-графика
    day_array = [day_distribution.get(d, 0) for d in range(1, 32)]

    # 7. Top flag types
    top_flags = flag_counter.most_common(10)

    return {
        "period": {"id": period.id, "name": period.name, "is_active": bool(period.is_active)},
        "eligible_residents": eligible_count,
        "missing_count": max(0, eligible_count - len(readings)),
        "submitted_count": len(readings),
        "submission_rate_pct": (
            round(len(readings) / eligible_count * 100, 1) if eligible_count else 0.0
        ),
        "by_source": dict(by_source),
        "anomalies": {
            "flagged_count": flagged,
            "flagged_pct": (
                round(flagged / len(readings) * 100, 1) if readings else 0.0
            ),
            "avg_score": round(avg_score, 1),
            "max_score": max_score,
            "top_flags": [{"flag": f, "count": c} for f, c in top_flags],
        },
        "totals": totals_summary,
        "day_distribution": day_array,
    }
