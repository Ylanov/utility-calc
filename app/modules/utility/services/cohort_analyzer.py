"""cohort_analyzer.py — peer-сравнение жильцов по группам (когортам).

Расширение существующей peer-логики из anomaly_detector (HIGH_VS_PEERS).
Раньше peer = «жильцы той же комнаты». Теперь — несколько разрезов:

  - by dormitory  — все жильцы того же общежития
  - by family size — семьи такого же размера (1, 2, 3-4, 5+ человек)
  - by area bucket — комнаты схожей площади (small/medium/large по quartile)

Для каждой когорты в выбранном периоде считает:
  - count, median, p95, max того что считаем (total_cost / hot_water /
    cold_water / electricity)
  - outliers: жильцы с показателем > 2× median (адаптивный порог,
    лучше абсолютного лимита для разных общежитий с разными режимами)

Используется через /api/admin/analyzer/cohorts?period_id=N&metric=total_cost
"""
from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal
from statistics import median
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.modules.utility.models import (
    BillingPeriod,
    MeterReading,
    Room,
    User,
)

logger = logging.getLogger(__name__)


# Допустимые метрики для cohort-анализа.
ALLOWED_METRICS = ("total_cost", "hot_water", "cold_water", "electricity")


def _family_bucket(rc: int) -> str:
    """Группировка жильцов по размеру семьи."""
    if rc <= 1:
        return "1 (одиночка)"
    if rc == 2:
        return "2 (пара)"
    if rc <= 4:
        return "3-4 (семья)"
    return "5+ (большая семья)"


def _area_bucket(area: float, quartiles: list[float]) -> str:
    """Группировка по quartile площади. quartiles — три точки [Q1, Q2, Q3]."""
    if not quartiles or len(quartiles) < 3:
        return "—"
    if area <= quartiles[0]:
        return f"small (≤ {quartiles[0]:.0f} м²)"
    if area <= quartiles[1]:
        return f"medium ({quartiles[0]:.0f}-{quartiles[1]:.0f} м²)"
    if area <= quartiles[2]:
        return f"large ({quartiles[1]:.0f}-{quartiles[2]:.0f} м²)"
    return f"xlarge (> {quartiles[2]:.0f} м²)"


def _stats(values: list[Decimal]) -> dict:
    """Возвращает count/median/p95/max/min для списка значений."""
    if not values:
        return {"count": 0, "median": None, "p95": None, "max": None, "min": None}
    xs = sorted(values)
    p95_idx = int(0.95 * (len(xs) - 1))
    return {
        "count": len(xs),
        "median": float(median(xs)),
        "p95": float(xs[p95_idx]),
        "max": float(xs[-1]),
        "min": float(xs[0]),
    }


async def analyze_cohorts(
    db,
    period_id: int,
    metric: str = "total_cost",
    outlier_factor: float = 2.0,
) -> dict:
    """Сравнение жильцов в когортах по выбранной метрике.

    Возвращает dict:
      {
        "period": {...},
        "metric": "total_cost",
        "outlier_factor": 2.0,
        "by_dormitory": [
          {"key": "4дв.стр.5", "stats": {...}, "outliers": [...]}
        ],
        "by_family_size": [...],
        "by_area_bucket": [...],
      }
    """
    if metric not in ALLOWED_METRICS:
        return {
            "fatal": f"metric={metric!r} не поддерживается. "
                     f"Допустимо: {ALLOWED_METRICS}",
        }

    period = await db.get(BillingPeriod, period_id)
    if not period:
        return {"fatal": f"period_id={period_id} не найден"}

    # Тянем все approved-readings + жильца + комнату одним запросом
    rows = (await db.execute(
        select(MeterReading)
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
        )
        .where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True),
        )
    )).scalars().all()

    if not rows:
        return {
            "period": {"id": period.id, "name": period.name},
            "metric": metric,
            "fatal": "В периоде нет approved-readings",
        }

    # Собираем (user_id, username, dormitory, area, residents_count, value)
    points: list[dict] = []
    for r in rows:
        user = r.user
        room = user.room if user else None
        if not user or not room:
            continue
        # Берём метрику с корректным None-handling
        raw = getattr(r, metric)
        if raw is None:
            continue
        try:
            value = Decimal(str(raw))
        except Exception:
            continue
        # Пропускаем нулевые — они искажают peer-comparison
        # (baseline-readings и т.п.).
        if value <= 0:
            continue
        points.append({
            "reading_id": r.id,
            "user_id": user.id,
            "username": user.username,
            "dormitory": room.dormitory_name or "—",
            "room_number": room.room_number or "—",
            "area": float(room.apartment_area or 0),
            "residents_count": int(user.residents_count or 1),
            "value": value,
        })

    if not points:
        return {
            "period": {"id": period.id, "name": period.name},
            "metric": metric,
            "fatal": "Нет данных с положительным значением метрики",
        }

    # Определяем quartiles по площади для области-bucket
    areas = sorted(p["area"] for p in points if p["area"] > 0)
    if len(areas) >= 4:
        q1 = areas[len(areas) // 4]
        q2 = areas[len(areas) // 2]
        q3 = areas[3 * len(areas) // 4]
        quartiles = [q1, q2, q3]
    else:
        quartiles = []

    # Группировка
    grouped: dict[str, dict[str, list[dict]]] = {
        "by_dormitory": defaultdict(list),
        "by_family_size": defaultdict(list),
        "by_area_bucket": defaultdict(list),
    }
    for p in points:
        grouped["by_dormitory"][p["dormitory"]].append(p)
        grouped["by_family_size"][_family_bucket(p["residents_count"])].append(p)
        if quartiles:
            grouped["by_area_bucket"][_area_bucket(p["area"], quartiles)].append(p)

    def _build_section(group_dict: dict[str, list[dict]]) -> list[dict]:
        result = []
        for key, items in sorted(group_dict.items()):
            values = [item["value"] for item in items]
            stats = _stats(values)
            outliers: list[dict] = []
            if stats["median"]:
                threshold = Decimal(str(stats["median"])) * Decimal(str(outlier_factor))
                outliers = [
                    {
                        "reading_id": it["reading_id"],
                        "user_id": it["user_id"],
                        "username": it["username"],
                        "dormitory": it["dormitory"],
                        "room": it["room_number"],
                        "value": float(it["value"]),
                        "ratio_to_median": float(it["value"]) / stats["median"]
                            if stats["median"] else None,
                    }
                    for it in items
                    if it["value"] > threshold
                ]
                # Сортируем outliers по убыванию ratio
                outliers.sort(
                    key=lambda x: x.get("ratio_to_median") or 0, reverse=True
                )
                outliers = outliers[:10]  # топ-10 на группу
            result.append({
                "key": key,
                "stats": stats,
                "outliers": outliers,
                "outliers_count": len(outliers),
            })
        return result

    return {
        "period": {"id": period.id, "name": period.name},
        "metric": metric,
        "outlier_factor": outlier_factor,
        "total_points": len(points),
        "by_dormitory": _build_section(grouped["by_dormitory"]),
        "by_family_size": _build_section(grouped["by_family_size"]),
        "by_area_bucket": _build_section(grouped["by_area_bucket"]),
    }
