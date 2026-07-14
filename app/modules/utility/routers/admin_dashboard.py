# app/modules/utility/routers/admin_dashboard.py

import logging
from typing import Optional
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, desc

from app.core.database import get_db
from app.core.request_context import current_request_id
from app.modules.utility.models import (
    User, AuditLog
)
from app.core.dependencies import RoleChecker

router = APIRouter(tags=["Admin Dashboard"])
logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")
allow_dashboard = RoleChecker(["accountant", "admin", "financier"])


# =====================================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: Запись в журнал действий
# =====================================================================
async def write_audit_log(
    db: AsyncSession,
    user_id: int,
    username: str,
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    details: Optional[dict] = None
):
    """
    Универсальная функция записи действия в журнал.
    Импортируется и используется в любом роутере:

        from app.modules.utility.routers.admin_dashboard import write_audit_log
        await write_audit_log(db, user.id, user.username, "approve", "reading", reading.id, {"total": 1234})
    """
    try:
        # Включаем request_id в детали — связка action ↔ HTTP-запрос ↔ логи.
        # Если детали уже заданы, не перезаписываем (детали важнее).
        merged_details = dict(details or {})
        rid = current_request_id.get()
        if rid and rid != "-" and "request_id" not in merged_details:
            merged_details["request_id"] = rid

        log_entry = AuditLog(
            user_id=user_id,
            username=username,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=merged_details or None,
        )
        db.add(log_entry)
        # НЕ делаем commit — он произойдёт в вызывающей транзакции.
        # Это гарантирует что запись в лог атомарна с основным действием.
    except Exception as e:
        # Логирование не должно ломать основную операцию
        logger.error(f"Failed to write audit log: {e}")


# =====================================================================
# ЗДОРОВЬЕ СИСТЕМЫ (сторож system_health_task, аудит 2026-07-14)
# =====================================================================
@router.get("/api/admin/system-health", summary="Алерты здоровья системы (баннер дашборда)")
async def get_system_health(
    current_user: User = Depends(allow_dashboard),
    db: AsyncSession = Depends(get_db),
):
    """Сводка сторожа (диск/релей ГИС/1С/очереди) из SystemSetting
    'system_health'. Если запись старше 30 мин (пишется каждые 10) —
    сами фоновые задачи мертвы (beat/worker) → это первый crit-алерт."""
    import json as _json
    from datetime import datetime as _dt
    from app.core.time_utils import utcnow as _utcnow
    from app.modules.utility.models import SystemSetting as _SS

    row = (await db.execute(
        select(_SS).where(_SS.key == "system_health")
    )).scalars().first()
    data = {}
    if row and row.value:
        try:
            data = _json.loads(row.value)
        except Exception:
            data = {}
    alerts = list(data.get("alerts") or [])
    checked_at = data.get("checked_at")
    stale = True
    if checked_at:
        try:
            stale = (_utcnow() - _dt.fromisoformat(checked_at)).total_seconds() > 1800
        except Exception:
            stale = True
    if stale:
        alerts.insert(0, {
            "level": "crit", "code": "beat_dead",
            "message": ("Фоновые задачи не выполняются (Celery beat/worker): сводка здоровья "
                        + (f"не обновлялась с {checked_at[:16]}" if checked_at else "ещё ни разу не писалась")
                        + ". Авто-сборы 1С/ГИС и авто-закрытие периодов стоят."),
        })
    return {"checked_at": checked_at, "stale": stale, "alerts": alerts}


# =====================================================================
# ЖУРНАЛ ДЕЙСТВИЙ (AUDIT LOG)
# =====================================================================
@router.get("/api/admin/audit-log", summary="Журнал действий администратора")
async def get_audit_log(
    page: int = Query(1, ge=1),
    limit: int = Query(30, ge=1, le=200),
    action: Optional[str] = Query(None, description="Фильтр по типу действия"),
    entity_type: Optional[str] = Query(None, description="Фильтр по типу сущности"),
    user_id: Optional[int] = Query(None, description="Фильтр по пользователю"),
    current_user: User = Depends(allow_dashboard),
    db: AsyncSession = Depends(get_db)
):
    """Постраничный журнал действий с фильтрацией."""
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))

    if action:
        query = query.where(AuditLog.action == action)
        count_query = count_query.where(AuditLog.action == action)

    if entity_type:
        query = query.where(AuditLog.entity_type == entity_type)
        count_query = count_query.where(AuditLog.entity_type == entity_type)

    if user_id:
        query = query.where(AuditLog.user_id == user_id)
        count_query = count_query.where(AuditLog.user_id == user_id)

    total = (await db.execute(count_query)).scalar_one()

    rows = (await db.execute(
        query.order_by(desc(AuditLog.created_at))
        .offset((page - 1) * limit)
        .limit(limit)
    )).scalars().all()

    items = []
    for log in rows:
        items.append({
            "id": log.id,
            "username": log.username,
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "details": log.details,
            "created_at": log.created_at.strftime("%d.%m.%Y %H:%M") if log.created_at else None,
        })

    return {"total": total, "page": page, "size": limit, "items": items}


@router.get("/api/admin/audit-log/actions", summary="Список типов действий для фильтра")
async def get_audit_actions(
    current_user: User = Depends(allow_dashboard),
    db: AsyncSession = Depends(get_db)
):
    """Возвращает уникальные типы действий и сущностей для фильтров."""
    actions_result = await db.execute(
        select(AuditLog.action, func.count(AuditLog.id).label("cnt"))
        .group_by(AuditLog.action)
        .order_by(desc("cnt"))
    )
    entities_result = await db.execute(
        select(AuditLog.entity_type, func.count(AuditLog.id).label("cnt"))
        .group_by(AuditLog.entity_type)
        .order_by(desc("cnt"))
    )
    return {
        "actions": [{"name": r[0], "count": r[1]} for r in actions_result.all()],
        "entities": [{"name": r[0], "count": r[1]} for r in entities_result.all()],
    }
