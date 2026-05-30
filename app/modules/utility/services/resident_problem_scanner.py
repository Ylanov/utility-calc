"""resident_problem_scanner.py — фоновый сканер реальных проблем жильцов.

Ядро системы сигнализации. Прогоняет детекторы по жильцам и upsert'ит
персистентные сигналы в таблицу resident_problems:
  • дедупликация по (user_id, problem_type) — один OPEN-сигнал;
  • повторный скан обновляет last_seen_at / score / severity;
  • исчезнувшая проблема авто-закрывается (status=resolved).

Переиспользует finance_analyzer (DEBT_GROWING/ZERO_BILL/BILL_SPIKE/OVERPAY)
и добавляет сигналы, которых там нет на уровне истории:
  • NOT_SUBMITTING — жилец перестал подавать показания N+ периодов;
  • HIGH_DEBT     — крупная задолженность сверх порога;
  • FORMAT_SUSPECT— битый формат (>99999, потеряна точка);
  • METER_FROZEN  — показания счётчика «замерли» 3+ периода.

«Активные жильцы» = те, у кого есть approved-readings за последние периоды
(есть baseline для сравнения).
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.time_utils import utcnow
from app.modules.utility.models import (
    BillingPeriod, MeterReading, ResidentProblem, User,
)
from app.modules.utility.services.finance_analyzer import analyze_finance

logger = logging.getLogger(__name__)

PERIODS_WINDOW = 6           # сколько последних периодов берём для истории
NOT_SUBMITTING_PERIODS = 2   # не подаёт N+ периодов подряд → сигнал
HIGH_DEBT_RUB = Decimal("5000")
FORMAT_THRESHOLD = Decimal("99999")
ZERO = Decimal("0")

# problem_type → (severity, человекочитаемый заголовок)
_PROBLEM_META = {
    "NOT_SUBMITTING":  ("high", "Не подаёт показания"),
    "DEBT_GROWING":    ("high", "Долг растёт"),
    "HIGH_DEBT":       ("high", "Крупная задолженность"),
    "ZERO_BILL":       ("medium", "Нулевой счёт при истории начислений"),
    "BILL_SPIKE":      ("medium", "Резкий скачок суммы"),
    "METER_FROZEN":    ("medium", "Счётчик «замер»"),
    "FORMAT_SUSPECT":  ("critical", "Битый формат показания"),
    "OVERPAY_SUSPECT": ("low", "Подозрительная переплата"),
    # Уровень КОМНАТЫ: состав жильцов не совпадает с типом квартиры
    # (несколько семей / холостяки без пометки / смешанные типы). severity и
    # title уточняются по kind через override в _upsert_problem; здесь — дефолт
    # для авто-резолва и fallback. См. services/room_audit.py.
    "ROOM_TYPE_MISMATCH": ("high", "Несоответствие типа квартиры"),
}

# Финансовые флаги finance_analyzer → наши problem_type.
# MISSING_RECEIPT не переносим — его точнее покрывает NOT_SUBMITTING.
_FINANCE_FLAG_MAP = {
    "DEBT_GROWING": "DEBT_GROWING",
    "ZERO_BILL": "ZERO_BILL",
    "BILL_SPIKE": "BILL_SPIKE",
    "OVERPAY_SUSPECT": "OVERPAY_SUSPECT",
}

_MONTHS_RU = {
    "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
    "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
}


def _chrono_key(period: BillingPeriod):
    """(год, месяц) из period.name «Май 2026» — хронологическая сортировка
    (period_id ненадёжен, периоды заводились не по календарю)."""
    name = (period.name or "").strip().lower().split()
    if len(name) == 2 and name[0] in _MONTHS_RU:
        try:
            return (int(name[1]), _MONTHS_RU[name[0]], period.id or 0)
        except (ValueError, TypeError):
            pass
    return (0, 0, period.id or 0)


def _D(v) -> Decimal:
    return Decimal(str(v)) if v is not None else ZERO


def _detect_for_user(
    user: User,
    readings_by_period: dict,        # period_id -> MeterReading (только этого user)
    periods_chrono: list,            # BillingPeriod, старые → новые
) -> list[dict]:
    """Возвращает список обнаруженных проблем для жильца:
    [{type, score, details}, ...]."""
    problems: list[dict] = []
    current_period = periods_chrono[-1]
    cur_reading = readings_by_period.get(current_period.id)

    # Хронологический ряд readings (None где нет подачи).
    series = [readings_by_period.get(p.id) for p in periods_chrono]
    submitted = [r for r in series if r is not None]

    # ---------- NOT_SUBMITTING ----------
    # «Реальная» подача = НЕ авто-добивка. auto_fill_missing_readings_task
    # ежедневно создаёт approved-readings с флагом AUTO_NORM/AUTO_GENERATED для
    # неподавших — если считать их подачей, NOT_SUBMITTING никогда не сработает
    # в закрытых периодах (всё «добито»). Поэтому для gap авто/ручные служебные
    # readings = «не подавал».
    def _is_real_submission(r) -> bool:
        if r is None:
            return False
        fl = r.anomaly_flags or ""
        return not any(m in fl for m in (
            "AUTO_NORM", "AUTO_GENERATED", "AUTO_NO_HISTORY", "MANUAL_RECEIPT"))

    real_submitted = [r for r in series if _is_real_submission(r)]
    gap = 0
    for r in reversed(series):
        if not _is_real_submission(r):
            gap += 1
        else:
            break
    # Сигналим только если жилец КОГДА-ТО реально подавал (есть baseline) и
    # реально молчит N+ периодов (авто-добивка молчанием не считается).
    if real_submitted and gap >= NOT_SUBMITTING_PERIODS:
        problems.append({
            "type": "NOT_SUBMITTING",
            "score": min(40 + gap * 10, 100),
            "details": {"periods_silent": gap,
                        "last_real_period": next(
                            (p.name for p, r in zip(periods_chrono[::-1], series[::-1])
                             if _is_real_submission(r)), None)},
        })

    # ---------- FORMAT_SUSPECT ----------
    if cur_reading and (
        _D(cur_reading.hot_water) > FORMAT_THRESHOLD
        or _D(cur_reading.cold_water) > FORMAT_THRESHOLD
    ):
        problems.append({
            "type": "FORMAT_SUSPECT",
            "score": 90,
            "details": {"hot_water": float(cur_reading.hot_water or 0),
                        "cold_water": float(cur_reading.cold_water or 0),
                        "total_cost": float(cur_reading.total_cost or 0)},
        })

    # ---------- HIGH_DEBT ----------
    if cur_reading:
        debt = _D(cur_reading.debt_209) + _D(cur_reading.debt_205)
        if debt > HIGH_DEBT_RUB:
            problems.append({
                "type": "HIGH_DEBT",
                "score": min(40 + int(debt / HIGH_DEBT_RUB) * 10, 100),
                "details": {"debt": float(debt)},
            })

    # ---------- METER_FROZEN ----------
    # Последние 3 подачи подряд с одинаковыми (hot, cold) при ненулевом расходе.
    if len(submitted) >= 3:
        last3 = submitted[-3:]
        hots = {(_D(r.hot_water), _D(r.cold_water)) for r in last3}
        if len(hots) == 1 and any(v > 0 for v in next(iter(hots))):
            problems.append({
                "type": "METER_FROZEN",
                "score": 35,
                "details": {"value": [float(x) for x in next(iter(hots))]},
            })

    # ---------- Финансовые правила (переиспользуем finance_analyzer) ----------
    prev_costs = [_D(r.total_cost) for r in submitted[:-1]] if cur_reading else \
        [_D(r.total_cost) for r in submitted]
    prev_debts = [
        _D(r.debt_209) + _D(r.debt_205) for r in submitted[:-1]
    ] if cur_reading else [_D(r.debt_209) + _D(r.debt_205) for r in submitted]
    cur_debt = (_D(cur_reading.debt_209) + _D(cur_reading.debt_205)) if cur_reading else ZERO
    cur_over = (_D(cur_reading.overpayment_209) + _D(cur_reading.overpayment_205)) \
        if cur_reading else ZERO

    fin_flags, fin_score = analyze_finance(
        user_id=user.id,
        residents_count=getattr(user, "residents_count", 1) or 1,
        current_total_cost=_D(cur_reading.total_cost) if cur_reading else None,
        current_debt=cur_debt,
        current_overpayment=cur_over,
        prev_costs=prev_costs,
        prev_debts=prev_debts,
        has_reading=cur_reading is not None,
        resident_type=getattr(user, "resident_type", "family") or "family",
        billing_mode=getattr(user, "billing_mode", "by_meter") or "by_meter",
    )
    for f in fin_flags:
        ptype = _FINANCE_FLAG_MAP.get(f)
        if ptype and not any(p["type"] == ptype for p in problems):
            problems.append({
                "type": ptype,
                "score": fin_score,
                "details": {"source": "finance_analyzer", "flag": f},
            })

    return problems


async def scan_resident_problems(db: AsyncSession) -> dict:
    """Главная точка входа. Прогоняет детекторы по всем жильцам с историей
    и синхронизирует resident_problems. Возвращает сводку."""
    scan_start = utcnow()

    # 1. Последние периоды (хронологически: старые → новые).
    all_periods = (await db.execute(select(BillingPeriod))).scalars().all()
    if not all_periods:
        return {"scanned_users": 0, "problems_open": 0, "skipped": "no_periods"}
    periods_chrono = sorted(all_periods, key=_chrono_key)[-PERIODS_WINDOW:]
    period_ids = [p.id for p in periods_chrono]

    # 2. Approved-readings за окно + жильцы.
    rows = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.user))
        .where(
            MeterReading.period_id.in_(period_ids),
            MeterReading.is_approved.is_(True),
        )
    )).scalars().all()

    # 3. Группируем: user_id -> {period_id: reading}, user_id -> User.
    by_user: dict[int, dict] = {}
    users: dict[int, User] = {}
    for r in rows:
        if not r.user_id or (r.user and r.user.is_deleted):
            continue
        by_user.setdefault(r.user_id, {})[r.period_id] = r
        if r.user:
            users[r.user_id] = r.user

    # 4. Детектируем + upsert.
    detected_keys: set[tuple] = set()
    open_count = 0
    for user_id, rbp in by_user.items():
        user = users.get(user_id)
        if not user:
            continue
        problems = _detect_for_user(user, rbp, periods_chrono)
        for p in problems:
            detected_keys.add((user_id, p["type"]))
            await _upsert_problem(db, user_id, p["type"], p["score"], p["details"])
            open_count += 1

    # 4b. Уровень КОМНАТЫ: несоответствие типа квартиры составу жильцов.
    #     Считаем по привязке User.room_id (а не по подачам), сигнал вешаем на
    #     представителя комнаты (min user_id) — один сигнал на проблемную
    #     квартиру. Изолируем try/except: сбой аудита не должен валить весь скан.
    room_mismatch_count = 0
    try:
        from app.modules.utility.services.room_audit import find_room_type_mismatches
        for m in await find_room_type_mismatches(db):
            rep_uid = m["representative_user_id"]
            detected_keys.add((rep_uid, "ROOM_TYPE_MISMATCH"))
            await _upsert_problem(
                db, rep_uid, "ROOM_TYPE_MISMATCH",
                score=75 if m["severity"] == "high" else 50,
                details={
                    "kind": m["kind"],
                    "room_id": m["room_id"],
                    "address": m["address"],
                    "n_residents": m["n_residents"],
                    "n_family": m["n_family"],
                    "n_single": m["n_single"],
                    "recommendation": m["recommendation"],
                    "residents": m["residents"],
                },
                severity=m["severity"],
                title=m["title"],
            )
            open_count += 1
            room_mismatch_count += 1
    except Exception:
        logger.exception("[resident_scan] room type audit failed")

    # 5. Авто-resolve: закрываем open/acknowledged сигналы, НЕ обнаруженные в
    #    этом скане. Исключаем detected_keys ЯВНО (tuple notin_) — нельзя
    #    полагаться только на last_seen_at < scan_start: при autoflush=False
    #    обновлённый в этом же скане last_seen_at ещё НЕ записан в БД к моменту
    #    UPDATE, и только что подтверждённый/обновлённый сигнал ложно попал бы
    #    под условие (флип-флоп каждые 6ч + откат acknowledge админа).
    #    Если в скане никого не нашли (пустое окно/сбой) — НЕ резолвим, чтобы
    #    не стереть разом всю историю активных сигналов. by_user покрывает
    #    per-user детекторы; detected_keys — на случай когда есть только
    #    room-level сигналы (аудит типа квартиры не зависит от подач).
    if by_user or detected_keys:
        resolve_stmt = (
            update(ResidentProblem)
            .where(
                ResidentProblem.status.in_(["open", "acknowledged"]),
                ResidentProblem.last_seen_at < scan_start,
                # Резолвим ТОЛЬКО типы, которые детектит ЭТОТ сканер — иначе
                # затёрли бы RECALC_DRIFT, который ведёт auto_recalc_drift.
                ResidentProblem.problem_type.in_(list(_PROBLEM_META.keys())),
            )
            .values(status="resolved", resolved_at=scan_start)
        )
        if detected_keys:
            resolve_stmt = resolve_stmt.where(
                tuple_(ResidentProblem.user_id, ResidentProblem.problem_type)
                .notin_(list(detected_keys))
            )
        await db.execute(resolve_stmt)
    await db.commit()

    logger.info(
        "[resident_scan] users=%d detected=%d", len(by_user), open_count
    )
    return {
        "scanned_users": len(by_user),
        "problems_detected": open_count,
        "room_mismatches": room_mismatch_count,
        "periods": [p.name for p in periods_chrono],
    }


async def _upsert_problem(
    db: AsyncSession, user_id: int, ptype: str, score: int, details: Optional[dict],
    severity: Optional[str] = None, title: Optional[str] = None,
) -> None:
    sev_default, title_default = _PROBLEM_META.get(ptype, ("medium", ptype))
    severity = severity or sev_default
    title = title or title_default
    now = utcnow()
    existing = (await db.execute(
        select(ResidentProblem).where(
            ResidentProblem.user_id == user_id,
            ResidentProblem.problem_type == ptype,
            ResidentProblem.status != "resolved",
        )
    )).scalars().first()
    if existing:
        existing.last_seen_at = now
        existing.score = score
        existing.severity = severity
        existing.title = title
        existing.details = details
    else:
        db.add(ResidentProblem(
            user_id=user_id, problem_type=ptype, severity=severity,
            score=score, title=title, details=details, status="open",
            first_detected_at=now, last_seen_at=now,
        ))


__all__ = ["scan_resident_problems"]
