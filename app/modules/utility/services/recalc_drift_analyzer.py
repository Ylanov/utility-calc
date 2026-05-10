"""recalc_drift_analyzer.py — батчевый детектор расхождений расчёта.

Что делает: для каждого approved-reading'а в указанном периоде делает
полный пересчёт через calculate_utilities (точно так же как кнопка
«Проверить расчёт» в админке делает для одной квитанции) и сравнивает
с тем что хранится в БД. Если расхождение больше порога — добавляет в
список «drifted» с диагностикой.

Зачем: после изменения тарифа задним числом, ручных правок БД, багов
формул — часть reading'ов может «уехать» от текущего расчёта. Этот
анализатор находит их батчем по периоду — раньше пришлось бы кликать
«Проверить» по каждой строке вручную.

Используется в /api/admin/analyzer/recalc-drift?period_id=N.

Это не «sanity»-анализатор (как reading_validators) и не «аномалия» в
поведенческом смысле (как anomaly_detector). Это AUDIT-анализатор —
ловит расхождение между текущей формулой расчёта и сохранённым
результатом, без оценки «правильности» исходных данных.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.modules.utility.models import (
    BillingPeriod,
    MeterReading,
    Room,
    Tariff,
    User,
)
from app.modules.utility.services.calculations import CalculationError
from app.modules.utility.services.reading_calculator import (
    compute_reading_breakdown,
)
from app.modules.utility.services.tariff_cache import tariff_cache

logger = logging.getLogger(__name__)


# Порог считается «расхождением». Меньше копейки — округление при
# разных проходах. Можно ужесточить до 0.001 если нужно ловить даже
# малейшие отличия (например, при расследовании). По умолчанию 1 коп —
# в БД суммы с 2 знаками, и 1 коп уже видна в квитанции.
DEFAULT_DRIFT_THRESHOLD = Decimal("0.01")


async def detect_drift_in_period(
    db,
    period_id: int,
    threshold: Optional[Decimal] = None,
    limit: Optional[int] = None,
) -> dict:
    """Анализирует все approved-reading'и в periоде.

    Возвращает dict:
      {
        "period": {"id": ..., "name": ...},
        "checked": int,            # сколько reading'ов проверено
        "drifted": [               # reading'и с расхождением
            {
              "reading_id": int, "user_id": int, "username": str,
              "stored_total": "1234.56", "calc_total": "1280.00",
              "diff": "+45.44", "diff_pct": "+3.7%",
              "reason": "ok" | "calc_error" | "no_tariff" | "no_room",
            },
            ...
        ],
        "errors": [...],           # для которых расчёт упал (CalcError)
        "stats": {
            "total_diff_sum": "...",   # сумма всех diff (со знаком)
            "abs_diff_sum": "...",     # сумма abs(diff) — масштаб «дрейфа»
            "max_diff_abs": "...",
        },
      }
    """
    thresh = threshold if threshold is not None else DEFAULT_DRIFT_THRESHOLD

    period = await db.get(BillingPeriod, period_id)
    if not period:
        return {
            "period": None, "checked": 0, "drifted": [], "errors": [],
            "stats": {}, "fatal": f"period_id={period_id} не найден",
        }

    # Тянем reading'и + жильца + комнату + период одним запросом, чтобы
    # не делать N отдельных запросов в цикле (классический N+1).
    stmt = (
        select(MeterReading)
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
            selectinload(MeterReading.period),
        )
        .where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True),
        )
        .order_by(MeterReading.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    readings = list((await db.execute(stmt)).scalars().all())

    drifted: list[dict] = []
    errors: list[dict] = []
    total_diff_sum = Decimal("0")
    abs_diff_sum = Decimal("0")
    max_abs = Decimal("0")
    checked = 0

    for r in readings:
        user = r.user
        room = user.room if user else None

        # Reading без жильца/комнаты — реально такого быть не должно,
        # но не падаем.
        if not user or not room:
            errors.append({
                "reading_id": r.id, "reason": "no_user_or_room",
            })
            continue

        # Тариф — через тот же кеш, что использует основной расчёт.
        tariff = tariff_cache.get_effective_tariff(user=user, room=room)
        if tariff is None:
            tariff = (await db.execute(
                select(Tariff).where(Tariff.is_active.is_(True))
            )).scalars().first()
        if tariff is None:
            errors.append({
                "reading_id": r.id, "user_id": user.id,
                "reason": "no_active_tariff",
            })
            continue

        # Prev — хронологически предыдущий, по period_id (см. инцидент may 2026).
        prev_q = await db.execute(
            select(MeterReading)
            .options(selectinload(MeterReading.period))
            .where(
                MeterReading.user_id == user.id,
                MeterReading.room_id == room.id,
                MeterReading.is_approved.is_(True),
                MeterReading.period_id < r.period_id,
            )
            .order_by(MeterReading.period_id.desc())
            .limit(1)
        )
        prev = prev_q.scalars().first()

        # Считаем — может упасть на CalculationError при пустом тарифе.
        try:
            breakdown = compute_reading_breakdown(
                user=user, room=room, tariff=tariff,
                current_hot=r.hot_water or 0,
                current_cold=r.cold_water or 0,
                current_elect=r.electricity or 0,
                prev_reading=prev,
            )
        except CalculationError as e:
            errors.append({
                "reading_id": r.id, "user_id": user.id,
                "reason": f"calc_error: {e}",
            })
            continue

        checked += 1
        stored = Decimal(str(r.total_cost or 0))
        calc = Decimal(str(breakdown["total_cost"]))
        diff = calc - stored
        abs_diff = abs(diff)
        total_diff_sum += diff
        abs_diff_sum += abs_diff
        if abs_diff > max_abs:
            max_abs = abs_diff

        if abs_diff > thresh:
            diff_pct = None
            if stored != 0:
                pct = float(diff / stored * 100)
                diff_pct = f"{pct:+.1f}%"
            drifted.append({
                "reading_id": r.id,
                "user_id": user.id,
                "username": user.username,
                "room_number": room.room_number,
                "dormitory_name": room.dormitory_name,
                "stored_total": f"{stored:.2f}",
                "calc_total": f"{calc:.2f}",
                "diff": f"{diff:+.2f}",
                "diff_pct": diff_pct,
                "anomaly_flags": r.anomaly_flags,
                "is_baseline": breakdown["is_baseline"],
            })

    # Сортируем по abs(diff) убывания — самые большие расхождения сверху.
    drifted.sort(
        key=lambda x: abs(Decimal(x["diff"].replace("+", ""))), reverse=True
    )

    return {
        "period": {"id": period.id, "name": period.name},
        "checked": checked,
        "drifted_count": len(drifted),
        "drifted": drifted,
        "errors": errors,
        "errors_count": len(errors),
        "threshold": f"{thresh}",
        "stats": {
            "total_diff_sum": f"{total_diff_sum:+.2f}",
            "abs_diff_sum": f"{abs_diff_sum:.2f}",
            "max_diff_abs": f"{max_abs:.2f}",
        },
    }
