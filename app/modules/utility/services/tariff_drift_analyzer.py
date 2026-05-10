"""tariff_drift_analyzer.py — детектор «застрявших» reading'ов после
изменения тарифа задним числом.

Сценарий: админ меняет ставку (например, обновляет water_supply с 40 ₽
до 45 ₽), но reading'и за прошлые периоды остались с расчётом по старой
ставке (40 ₽). Если тариф изменён ЗАДНИМ числом (с valid_from в прошлом
или updated_at позже reading.created_at), то часть квитанций «устарела».

Этот анализатор быстро (без полного пересчёта) находит «подозрительные»
reading'и: те, что созданы ДО последнего изменения активного тарифа.
Дальше админ может запустить полный recalc-drift или массовый recalc.

Отличие от recalc_drift_analyzer:
  - tariff_drift — быстрый, не делает calculate_utilities (просто SQL по
    датам). Отвечает на вопрос «что МОГЛО устареть».
  - recalc_drift — медленный, реально пересчитывает и сравнивает.
    Отвечает на «что РЕАЛЬНО устарело и насколько».
Использовать: сначала tariff_drift как cheap-screening, потом recalc_drift
для точной оценки.

Используется в /api/admin/analyzer/tariff-drift.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, select

from app.modules.utility.models import (
    BillingPeriod,
    MeterReading,
    Tariff,
)

logger = logging.getLogger(__name__)


async def analyze_tariff_drift(
    db,
    tariff_id: Optional[int] = None,
    period_id: Optional[int] = None,
) -> dict:
    """Возвращает сводку по «застрявшим» reading'ам.

    Параметры:
      tariff_id — конкретный тариф для анализа. None = активный тариф.
      period_id — ограничить периодом. None = все периоды.

    Возвращает dict:
      {
        "tariff": {...},
        "tariff_last_change": "2026-05-01T12:00:00",
        "drifted_total": int,
        "drifted_by_period": [{"period_id", "period_name", "count"}, ...],
        "drifted_oldest": "2026-04-15T...",
      }
    """
    # 1. Выбираем тариф
    if tariff_id is not None:
        tariff = await db.get(Tariff, tariff_id)
    else:
        tariff = (await db.execute(
            select(Tariff)
            .where(Tariff.is_active.is_(True))
            .order_by(Tariff.id.desc())
            .limit(1)
        )).scalars().first()

    if not tariff:
        return {"fatal": "Активный тариф не найден"}

    # 2. Когда тариф последний раз менялся — берём максимум из valid_from
    # и updated_at (если updated_at заполнен — приоритетнее).
    last_change = tariff.updated_at or tariff.valid_from
    if last_change is None:
        return {
            "tariff": {"id": tariff.id, "name": tariff.name},
            "fatal": "У тарифа нет ни valid_from, ни updated_at — drift не определить",
        }

    # 3. Считаем reading'и созданные ДО последнего изменения тарифа.
    # Это «потенциально устаревшие» — расчёт мог отличаться от того,
    # что бы дал текущий тариф.
    base_query = select(MeterReading).where(
        MeterReading.is_approved.is_(True),
        MeterReading.created_at < last_change,
    )
    if period_id is not None:
        base_query = base_query.where(MeterReading.period_id == period_id)

    # Total count
    count_q = select(func.count()).select_from(base_query.subquery())
    drifted_total = (await db.execute(count_q)).scalar_one()

    # Group by period — сколько в каждом периоде
    by_period_q = (
        select(
            MeterReading.period_id,
            BillingPeriod.name.label("period_name"),
            func.count(MeterReading.id).label("cnt"),
            func.min(MeterReading.created_at).label("oldest"),
        )
        .select_from(MeterReading)
        .join(BillingPeriod, BillingPeriod.id == MeterReading.period_id, isouter=True)
        .where(
            MeterReading.is_approved.is_(True),
            MeterReading.created_at < last_change,
        )
    )
    if period_id is not None:
        by_period_q = by_period_q.where(MeterReading.period_id == period_id)
    by_period_q = (
        by_period_q
        .group_by(MeterReading.period_id, BillingPeriod.name)
        .order_by(MeterReading.period_id.desc())
    )
    rows = (await db.execute(by_period_q)).all()

    by_period = []
    overall_oldest = None
    for r in rows:
        by_period.append({
            "period_id": r.period_id,
            "period_name": r.period_name,
            "count": int(r.cnt),
            "oldest_reading_at": r.oldest.isoformat() if r.oldest else None,
        })
        if r.oldest and (overall_oldest is None or r.oldest < overall_oldest):
            overall_oldest = r.oldest

    return {
        "tariff": {
            "id": tariff.id,
            "name": tariff.name,
            "is_active": bool(tariff.is_active),
            "valid_from": tariff.valid_from.isoformat() if tariff.valid_from else None,
            "updated_at": tariff.updated_at.isoformat() if tariff.updated_at else None,
        },
        "tariff_last_change": last_change.isoformat(),
        "period_filter_id": period_id,
        "drifted_total": int(drifted_total),
        "drifted_oldest": overall_oldest.isoformat() if overall_oldest else None,
        "drifted_by_period": by_period,
        "advice": (
            "Нет дрейфа — все reading'и созданы ПОСЛЕ изменения тарифа."
            if drifted_total == 0 else
            f"Найдено {drifted_total} reading'ов, созданных до последнего "
            f"изменения тарифа ({last_change.strftime('%Y-%m-%d %H:%M')}). "
            "Запустите /api/admin/analyzer/recalc-drift по этим периодам, "
            "чтобы увидеть РЕАЛЬНЫЕ расхождения сумм."
        ),
    }
