"""auto_recalc_drift.py — авто-перерасчёт расхождений с предохранителями.

Идея (решение 30.05.2026): фоновая задача находит reading'и, где сохранённый
total разошёлся с текущей формулой (drift), и:
  • БЕЗОПАСНЫЙ случай → авто-применяет перерасчёт (приводит total к формуле);
  • ОПАСНЫЙ случай  → НЕ трогает, поднимает сигнал RECALC_DRIFT в Монитор
    проблем (колокольчик/Inbox) — пусть админ разберётся вручную.

Почему нельзя слепо пересчитывать ВСЁ: расхождение часто из-за БИТОГО ФОРМАТА
показаний (797205 вместо 797.205) — пересчёт по таким данным выставит жильцу
сотни тысяч ₽ (инцидент 1.48 млрд). Поэтому авто-фикс только когда:
  1) показания валидны (не >99999, иначе потеряна десятичная точка);
  2) пересчитанная сумма реалистична (< MAX_SAFE_TOTAL);
  3) у reading НЕТ ручных корректировок (иначе перерасчёт их сотрёт —
     compute_reading_breakdown не знает про Adjustment);
  4) это НЕ повторный фикс (маркер AUTO_RECALC_FIXED) — анти-цикл: если после
     прошлого авто-фикса снова drift, значит что-то перезаписывает/данные
     битые → сигнал, а не бесконечный пересчёт.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.orm import selectinload

from app.core.time_utils import utcnow
from app.modules.utility.models import (
    Adjustment, BillingPeriod, MeterReading, ResidentProblem, Tariff, User,
)
from app.modules.utility.services.calculations import CalculationError
from app.modules.utility.services.reading_calculator import compute_reading_breakdown
from app.modules.utility.services.tariff_cache import tariff_cache

logger = logging.getLogger(__name__)

DRIFT_THRESHOLD = Decimal("0.01")     # меньше копейки — округление, не drift
MAX_SAFE_TOTAL = Decimal("30000")     # выше — не авто-фиксим (вероятно баг данных)
FORMAT_THRESHOLD = Decimal("99999")   # показание выше = потеряна десятичная точка
AUTO_MARK = "AUTO_RECALC_FIXED"
ZERO = Decimal("0")


def _D(v) -> Decimal:
    return Decimal(str(v)) if v is not None else ZERO


async def auto_recalc_drift(db, period_id: int) -> dict:
    """Прогоняет активный (или указанный) период: безопасные drift — фиксит,
    опасные/повторные — сигналит. Возвращает сводку."""
    period = await db.get(BillingPeriod, period_id)
    if not period:
        return {"skipped": "no_period", "period_id": period_id}

    rows = (await db.execute(
        select(MeterReading)
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
            selectinload(MeterReading.period),
        )
        .where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True),
        )
    )).scalars().all()

    # Жильцы с ручными корректировками в этом периоде — их НЕ авто-фиксим
    # (compute_reading_breakdown не учитывает Adjustment, перерасчёт сотрёт их).
    adj_user_ids = set((await db.execute(
        select(Adjustment.user_id).where(Adjustment.period_id == period_id).distinct()
    )).scalars().all())

    from app.modules.utility.routers.settings import _load_seasonal
    seasonal = await _load_seasonal(db)

    fixed = 0
    signaled = 0
    checked = 0
    signaled_ids: set[int] = set()
    scan_ts = utcnow()

    for r in rows:
        user = r.user
        room = user.room if user else None
        if not user or not room:
            continue
        tariff = tariff_cache.get_effective_tariff(user=user, room=room)
        if tariff is None:
            tariff = (await db.execute(
                select(Tariff).where(Tariff.is_active.is_(True))
            )).scalars().first()
        if tariff is None:
            continue

        prev = (await db.execute(
            select(MeterReading)
            .where(
                MeterReading.user_id == user.id,
                MeterReading.room_id == room.id,
                MeterReading.is_approved.is_(True),
                MeterReading.period_id < r.period_id,
            )
            .order_by(MeterReading.period_id.desc())
            .limit(1)
        )).scalars().first()

        heating = seasonal.heating_season_active and tariff.is_heating_active_now()
        hw = seasonal.hot_water_heating_active and tariff.is_hw_heating_active_now()
        try:
            bd = compute_reading_breakdown(
                user=user, room=room, tariff=tariff,
                current_hot=r.hot_water or 0,
                current_cold=r.cold_water or 0,
                current_elect=r.electricity or 0,
                prev_reading=prev,
                heating_season_active=heating,
                hot_water_heating_active=hw,
            )
        except CalculationError:
            continue

        checked += 1
        stored = _D(r.total_cost)
        calc = _D(bd["total_cost"])
        if abs(calc - stored) <= DRIFT_THRESHOLD:
            continue  # расхождения нет

        # --- ЕСТЬ расхождение. Предохранители перед авто-фиксом ---
        flags = r.anomaly_flags or ""
        is_format_bad = (_D(r.hot_water) > FORMAT_THRESHOLD
                         or _D(r.cold_water) > FORMAT_THRESHOLD)
        is_huge = calc > MAX_SAFE_TOTAL
        already_fixed = AUTO_MARK in flags
        has_adjustments = user.id in adj_user_ids

        if is_format_bad or is_huge or already_fixed or has_adjustments:
            reason = (
                "format_suspect" if is_format_bad else
                "huge_sum" if is_huge else
                "repeat_drift" if already_fixed else
                "has_adjustments"
            )
            await _signal_recalc_drift(db, user, r, stored, calc, reason, scan_ts)
            signaled_ids.add(user.id)
            signaled += 1
        else:
            # БЕЗОПАСНО: применяем перерасчёт (привести total к формуле).
            for k in ("cost_hot_water", "cost_cold_water", "cost_sewage",
                      "cost_electricity", "cost_maintenance", "cost_social_rent",
                      "cost_waste", "cost_fixed_part"):
                if k in bd:
                    setattr(r, k, bd[k])
            r.total_209 = bd["total_209"]
            r.total_205 = bd["total_205"]
            r.total_cost = bd["total_cost"]  # триггер тоже выставит, дублируем явно
            # Маркер анти-цикла: при повторном drift в след. скане → сигнал.
            r.anomaly_flags = (flags + "," + AUTO_MARK).strip(",") if flags else AUTO_MARK
            fixed += 1
            logger.info(
                "[auto_recalc] fixed reading=%s user=%s %.2f→%.2f",
                r.id, user.id, float(stored), float(calc),
            )

    # Авто-resolve RECALC_DRIFT, исчезнувшие в этом прогоне (drift пропал —
    # пофикшен авто или исправлен вручную). Резолвим ТОЛЬКО свой тип
    # (RECALC_DRIFT), чтобы не трогать сигналы scan_resident_problems. И только
    # если реально сканировали (rows непусто) — иначе при пустом периоде стёрли
    # бы все сигналы. Исключаем signaled_ids явно (autoflush=False: их last_seen
    # ещё не в БД к моменту UPDATE).
    if rows:
        resolve_stmt = update(ResidentProblem).where(
            ResidentProblem.problem_type == "RECALC_DRIFT",
            ResidentProblem.status.in_(["open", "acknowledged"]),
        )
        if signaled_ids:
            resolve_stmt = resolve_stmt.where(
                ResidentProblem.user_id.notin_(list(signaled_ids)))
        await db.execute(resolve_stmt.values(status="resolved", resolved_at=scan_ts))

    await db.commit()
    logger.info(
        "[auto_recalc] period=%s checked=%d fixed=%d signaled=%d",
        period_id, checked, fixed, signaled,
    )
    return {
        "period": period.name,
        "checked": checked,
        "fixed": fixed,
        "signaled": signaled,
    }


async def _signal_recalc_drift(db, user, reading, stored, calc, reason, now):
    """Создаёт/обновляет сигнал RECALC_DRIFT в Мониторе проблем жильцов."""
    severity = "critical" if reason in ("format_suspect", "huge_sum") else "high"
    score = 90 if severity == "critical" else 60
    reason_ru = {
        "format_suspect": "битый формат показаний (потеряна точка)",
        "huge_sum": "пересчёт даёт нереальную сумму",
        "repeat_drift": "повторное расхождение после авто-перерасчёта",
        "has_adjustments": "есть ручные корректировки — нужен ручной перерасчёт",
    }.get(reason, reason)
    details = {
        "reading_id": reading.id,
        "stored_total": float(stored),
        "calc_total": float(calc),
        "diff": float(calc - stored),
        "reason": reason,
        "reason_ru": reason_ru,
    }
    existing = (await db.execute(
        select(ResidentProblem).where(
            ResidentProblem.user_id == user.id,
            ResidentProblem.problem_type == "RECALC_DRIFT",
            ResidentProblem.status != "resolved",
        )
    )).scalars().first()
    title = "Расхождение расчёта (нужна проверка)"
    if existing:
        existing.last_seen_at = now
        existing.score = score
        existing.severity = severity
        existing.title = title
        existing.details = details
    else:
        db.add(ResidentProblem(
            user_id=user.id, problem_type="RECALC_DRIFT", severity=severity,
            score=score, title=title, details=details, status="open",
            first_detected_at=now, last_seen_at=now,
        ))


__all__ = ["auto_recalc_drift"]
