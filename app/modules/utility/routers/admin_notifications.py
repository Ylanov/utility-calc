"""Уведомления для админа — сводка событий требующих внимания.

Это лёгкий poll-endpoint (без websocket'ов). Фронт делает запрос каждые
30 секунд, обновляет badge на колокольчике в шапке. При клике видно
список последних событий с глубокими ссылками.

Категории:
  - gsheets_conflicts — строки импорта в статусе conflict (нужна ручная обработка)
  - gsheets_unmatched — fuzzy-matcher не нашёл жильца (нужен reassign)
  - deletion_requests — заявки на удаление ПД (из audit_log)
  - anomalies — readings с высоким anomaly_score (не утверждены и не dismissed)

Не используем отдельную таблицу Notifications — нет необходимости. Все
события уже есть в БД, мы их просто агрегируем «на лету» одним запросом.
"""
from datetime import timedelta
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.time_utils import utcnow
from app.modules.utility.models import (
    User, GSheetsImportRow, AuditLog, MeterReading, SupportTicket,
    ResidentProblem,
)
from app.core.dependencies import RoleChecker


router = APIRouter(prefix="/api/admin", tags=["Admin Notifications"])

# Те же роли что и для дашборда — все админы видят уведомления.
allow_dashboard = RoleChecker(["accountant", "admin", "financier"])


@router.get("/notifications")
async def get_notifications(
    recent_hours: int = Query(72, ge=1, le=720, description="Окно «новых» событий, часов"),
    limit: int = Query(20, ge=1, le=100, description="Лимит на категорию"),
    current_user: User = Depends(allow_dashboard),
    db: AsyncSession = Depends(get_db),
):
    """Сводка событий требующих внимания.

    Окно `recent_hours` (default 72 = 3 дня) — берём только относительно
    свежие, чтобы badge не показывал «999» для старых проблем которые
    никто не закрыл.
    """
    cutoff = utcnow() - timedelta(hours=recent_hours)

    # 1) GSheets conflicts — счётчик + последние записи
    gsheets_conflict_count = (await db.execute(
        select(func.count(GSheetsImportRow.id))
        .where(GSheetsImportRow.status == "conflict")
    )).scalar_one()
    gsheets_unmatched_count = (await db.execute(
        select(func.count(GSheetsImportRow.id))
        .where(GSheetsImportRow.status == "unmatched")
    )).scalar_one()

    gsheets_recent = (await db.execute(
        select(GSheetsImportRow)
        .where(GSheetsImportRow.status.in_(["conflict", "unmatched"]))
        .order_by(GSheetsImportRow.created_at.desc())
        .limit(limit)
    )).scalars().all()

    # 2) Запросы на удаление ПД (из audit_log)
    deletion_count = (await db.execute(
        select(func.count(AuditLog.id))
        .where(
            AuditLog.action == "data_deletion_request",
            AuditLog.created_at >= cutoff,
        )
    )).scalar_one()
    deletion_recent = (await db.execute(
        select(AuditLog)
        .where(AuditLog.action == "data_deletion_request")
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )).scalars().all()

    # 3) Аномалии в показаниях (score > 50, не утверждены)
    anomaly_count = (await db.execute(
        select(func.count(MeterReading.id))
        .where(
            MeterReading.anomaly_score > 50,
            MeterReading.is_approved.is_(False),
            MeterReading.created_at >= cutoff,
        )
    )).scalar_one()

    # 4) Открытые обращения жильцов (support tickets).
    tickets_open_count = (await db.execute(
        select(func.count(SupportTicket.id))
        .where(SupportTicket.status.in_(["open", "in_progress"]))
    )).scalar_one()
    tickets_recent = (await db.execute(
        select(SupportTicket)
        .where(SupportTicket.status.in_(["open", "in_progress"]))
        .order_by(SupportTicket.created_at.desc())
        .limit(limit)
    )).scalars().all()

    # 5) Заявки на сброс пароля (из audit_log).
    # Не оставляем «вечный» бэйдж — фильтр по recent_hours.
    password_reset_count = (await db.execute(
        select(func.count(AuditLog.id))
        .where(
            AuditLog.action == "password_reset_request",
            AuditLog.created_at >= cutoff,
        )
    )).scalar_one()
    password_reset_recent = (await db.execute(
        select(AuditLog)
        .where(AuditLog.action == "password_reset_request")
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )).scalars().all()

    # 6) DATA_OVERFLOW_RESET readings — outliers сброшенные авто-cleanup'ом
    #    (celery beat cleanup_outlier_readings_task) или старым скриптом
    #    cleanup_anomaly_readings.py. Ждут разбора админа: удалить/принять
    #    с коррекцией/попросить жильца переподать.
    overflow_count = (await db.execute(
        select(func.count(MeterReading.id))
        .where(
            MeterReading.anomaly_flags.contains("DATA_OVERFLOW_RESET"),
            MeterReading.is_approved.is_(False),
        )
    )).scalar_one()
    overflow_recent = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.user))
        .where(
            MeterReading.anomaly_flags.contains("DATA_OVERFLOW_RESET"),
            MeterReading.is_approved.is_(False),
        )
        .order_by(MeterReading.created_at.desc())
        .limit(limit)
    )).scalars().all()

    # 7) Проблемы жильцов (монитор сигнализации) — активные high+critical,
    #    не отложенные (snooze). Источник: фоновый scan_resident_problems.
    rp_filter = [
        ResidentProblem.status.in_(["open", "acknowledged"]),
        ResidentProblem.severity.in_(["high", "critical"]),
        or_(ResidentProblem.snooze_until.is_(None),
            ResidentProblem.snooze_until < utcnow()),
    ]
    resident_problem_count = (await db.execute(
        select(func.count(ResidentProblem.id)).where(*rp_filter)
    )).scalar_one()
    resident_problem_recent = (await db.execute(
        select(ResidentProblem)
        .options(selectinload(ResidentProblem.user).selectinload(User.room))
        .where(*rp_filter)
        .order_by(ResidentProblem.score.desc())
        .limit(limit)
    )).scalars().all()

    def _iso(dt):
        return dt.isoformat() if dt else None

    return {
        "total": (
            gsheets_conflict_count + gsheets_unmatched_count
            + deletion_count + anomaly_count + tickets_open_count
            + password_reset_count + overflow_count + resident_problem_count
        ),
        "categories": {
            "gsheets_conflicts": {
                "count": gsheets_conflict_count,
                "label": "Конфликты импорта",
                "link": "/admin.html#tools?section=gsheets",
                "items": [
                    {
                        "id": r.id,
                        "title": f"{r.raw_fio or '—'} · комн. {r.raw_room_number or '—'}",
                        "subtitle": (r.conflict_reason or "")[:120],
                        "created_at": _iso(r.created_at),
                    }
                    for r in gsheets_recent if r.status == "conflict"
                ],
            },
            "gsheets_unmatched": {
                "count": gsheets_unmatched_count,
                "label": "Не сопоставлены жильцы",
                "link": "/admin.html#tools?section=gsheets",
                "items": [
                    {
                        "id": r.id,
                        "title": f"{r.raw_fio or '—'} · комн. {r.raw_room_number or '—'}",
                        "subtitle": "Жилец в БД не найден — нужно переназначить",
                        "created_at": _iso(r.created_at),
                    }
                    for r in gsheets_recent if r.status == "unmatched"
                ],
            },
            "deletion_requests": {
                "count": deletion_count,
                "label": "Запросы на удаление ПД",
                "link": "/admin.html#audit",
                "items": [
                    {
                        "id": a.id,
                        "title": f"От: {a.username or '—'}",
                        "subtitle": (a.details or {}).get("reason", "")[:120],
                        "created_at": _iso(a.created_at),
                    }
                    for a in deletion_recent
                ],
            },
            "anomalies": {
                "count": anomaly_count,
                "label": "Аномалии в показаниях",
                "link": "/admin.html#dashboard",
                "items": [],  # подробности — в дашборде
            },
            "tickets": {
                "count": tickets_open_count,
                "label": "Открытые обращения жильцов",
                "link": "/admin.html#tickets",
                "items": [
                    {
                        "id": t.id,
                        "title": (t.subject or "")[:80],
                        "subtitle": (t.message or "")[:120],
                        "created_at": _iso(t.created_at),
                    }
                    for t in tickets_recent
                ],
            },
            "password_resets": {
                "count": password_reset_count,
                "label": "Заявки на сброс пароля",
                "link": "/admin.html#users",
                "items": [
                    {
                        "id": a.id,
                        "title": (a.details or {}).get("full_name", "—"),
                        "subtitle": (
                            f"{(a.details or {}).get('dormitory_name', '')} "
                            f"ком. {(a.details or {}).get('room_number', '')} · "
                            f"тел/email: {(a.details or {}).get('contact', '—')}"
                        )[:160],
                        "created_at": _iso(a.created_at),
                    }
                    for a in password_reset_recent
                ],
            },
            "data_overflow_resets": {
                "count": overflow_count,
                "label": "Заблокированные показания (overflow)",
                "link": "/admin.html#tools?section=analyzer",
                "items": [
                    {
                        "id": r.id,
                        "title": (
                            f"{r.user.username if r.user else '—'} · "
                            f"период #{r.period_id}"
                        ),
                        "subtitle": (
                            f"hot={float(r.hot_water or 0):.3f} · "
                            f"cold={float(r.cold_water or 0):.3f} · "
                            f"elect={float(r.electricity or 0):.3f} — auto-cleanup "
                            f"вернул в черновики, ждёт ручного разбора"
                        )[:200],
                        "created_at": _iso(r.created_at),
                    }
                    for r in overflow_recent
                ],
            },
            "resident_problems": {
                "count": resident_problem_count,
                "label": "Проблемы жильцов",
                "link": "/admin.html#tools?section=analyzer",
                "items": [
                    {
                        "id": p.id,
                        "title": (
                            f"{p.title} · "
                            f"{p.user.username if p.user else '—'}"
                        ),
                        "subtitle": (
                            f"{p.user.room.dormitory_name if p.user and p.user.room else ''} "
                            f"ком. {p.user.room.room_number if p.user and p.user.room else ''} · "
                            f"важность: {p.severity}"
                        )[:160],
                        "created_at": _iso(p.first_detected_at),
                    }
                    for p in resident_problem_recent
                ],
            },
        },
    }
