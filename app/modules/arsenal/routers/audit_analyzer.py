"""Endpoints «Журнал действий» + «Центр анализа» для арсенала."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_arsenal_db
from app.modules.arsenal.deps import get_current_arsenal_user
from app.modules.arsenal.models import (
    ArsenalAnalyzerSetting,
    ArsenalAnomalyFlag,
    ArsenalAuditLog,
    ArsenalUser,
)

router = APIRouter(tags=["Arsenal Audit & Analyzer"])


def _require_admin(user: ArsenalUser) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Только для администратора")


# =====================================================================
# AUDIT LOG
# =====================================================================
@router.get("/audit-log")
async def list_audit(
    user_id: Optional[int] = Query(None),
    action: Optional[str] = Query(None),
    entity_type: Optional[str] = Query(None),
    entity_id: Optional[int] = Query(None),
    date_from: Optional[datetime] = Query(None),
    date_to: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    """Журнал действий с фильтрами. Admin видит всё; unit_head —
    только собственные действия (чтобы не подглядывать за другими)."""
    q = select(ArsenalAuditLog).order_by(ArsenalAuditLog.created_at.desc())
    cnt = select(func.count(ArsenalAuditLog.id))

    conditions = []
    if current_user.role != "admin":
        conditions.append(ArsenalAuditLog.user_id == current_user.id)
    if user_id:
        conditions.append(ArsenalAuditLog.user_id == user_id)
    if action:
        conditions.append(ArsenalAuditLog.action == action)
    if entity_type:
        conditions.append(ArsenalAuditLog.entity_type == entity_type)
    if entity_id is not None:
        conditions.append(ArsenalAuditLog.entity_id == entity_id)
    if date_from:
        conditions.append(ArsenalAuditLog.created_at >= date_from)
    if date_to:
        conditions.append(ArsenalAuditLog.created_at <= date_to)

    if conditions:
        q = q.where(and_(*conditions))
        cnt = cnt.where(and_(*conditions))

    total = (await db.execute(cnt)).scalar_one()
    rows = (await db.execute(q.limit(limit).offset(offset))).scalars().all()

    return {
        "total": total,
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": r.username,
                "action": r.action,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "details": r.details,
                "ip_address": r.ip_address,
                "created_at": r.created_at,
            }
            for r in rows
        ],
    }


@router.get("/audit-log/actions")
async def audit_action_catalog(
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    """Список уникальных action / entity_type из текущих логов — для UI-фильтров."""
    actions = (await db.execute(
        select(ArsenalAuditLog.action).distinct().limit(100)
    )).scalars().all()
    entities = (await db.execute(
        select(ArsenalAuditLog.entity_type).distinct().limit(100)
    )).scalars().all()
    return {"actions": sorted(actions), "entity_types": sorted(entities)}


# =====================================================================
# ANALYZER — settings
# =====================================================================
@router.get("/analyzer/settings")
async def list_analyzer_settings(
    category: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    _require_admin(current_user)
    q = select(ArsenalAnalyzerSetting).order_by(
        ArsenalAnalyzerSetting.category, ArsenalAnalyzerSetting.key
    )
    if category:
        q = q.where(ArsenalAnalyzerSetting.category == category)
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "key": r.key, "value": r.value, "value_type": r.value_type,
            "category": r.category, "description": r.description,
            "min_value": r.min_value, "max_value": r.max_value,
            "is_enabled": r.is_enabled, "updated_at": r.updated_at,
        }
        for r in rows
    ]


class SettingPatch(BaseModel):
    value: Optional[str] = None
    is_enabled: Optional[bool] = None


@router.patch("/analyzer/settings/{key}")
async def update_analyzer_setting(
    key: str,
    patch: SettingPatch,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    _require_admin(current_user)
    setting = await db.get(ArsenalAnalyzerSetting, key)
    if not setting:
        raise HTTPException(404, f"Настройка {key!r} не найдена")

    changes = {}
    if patch.value is not None and patch.value != setting.value:
        # Лёгкая валидация типа
        vt = setting.value_type
        val = patch.value.strip()
        try:
            if vt == "int":
                int(val)
            elif vt == "float":
                float(val.replace(",", "."))
            elif vt == "bool":
                if val.lower() not in ("true", "false", "1", "0", "yes", "no"):
                    raise ValueError("bool expected")
                val = "true" if val.lower() in ("true", "1", "yes") else "false"
        except ValueError:
            raise HTTPException(400, f"Значение не соответствует типу {vt}")
        changes["value"] = {"old": setting.value, "new": val}
        setting.value = val
    if patch.is_enabled is not None and patch.is_enabled != setting.is_enabled:
        changes["is_enabled"] = {"old": setting.is_enabled, "new": patch.is_enabled}
        setting.is_enabled = patch.is_enabled

    if not changes:
        return {"status": "noop"}

    setting.updated_at = datetime.utcnow()
    setting.updated_by_id = current_user.id

    # Аудит
    from app.modules.arsenal.services.audit import write_arsenal_audit
    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="update_analyzer_setting", entity_type="analyzer_setting",
        details={"key": key, "changes": changes},
    )
    await db.commit()
    return {"status": "ok", "changes": changes}


# =====================================================================
# ANALYZER — anomalies (список, dismiss, manual run)
# =====================================================================
@router.get("/analyzer/anomalies")
async def list_anomalies(
    rule_code: Optional[str] = Query(None),
    include_dismissed: bool = Query(False),
    include_resolved: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    q = select(ArsenalAnomalyFlag).order_by(
        ArsenalAnomalyFlag.severity.desc(),
        ArsenalAnomalyFlag.last_seen_at.desc(),
    )
    cnt = select(func.count(ArsenalAnomalyFlag.id))
    conds = []
    if rule_code:
        conds.append(ArsenalAnomalyFlag.rule_code == rule_code)
    if not include_dismissed:
        conds.append(ArsenalAnomalyFlag.dismissed_at.is_(None))
    if not include_resolved:
        conds.append(ArsenalAnomalyFlag.resolved_at.is_(None))
    if conds:
        q = q.where(and_(*conds))
        cnt = cnt.where(and_(*conds))

    total = (await db.execute(cnt)).scalar_one()
    rows = (await db.execute(q.limit(limit).offset(offset))).scalars().all()

    # Сводка по правилам (всегда полезно показать бейджи)
    by_rule_rows = (await db.execute(
        select(
            ArsenalAnomalyFlag.rule_code,
            ArsenalAnomalyFlag.severity,
            func.count(ArsenalAnomalyFlag.id),
        )
        .where(
            ArsenalAnomalyFlag.dismissed_at.is_(None),
            ArsenalAnomalyFlag.resolved_at.is_(None),
        )
        .group_by(ArsenalAnomalyFlag.rule_code, ArsenalAnomalyFlag.severity)
    )).all()
    summary: dict = {}
    for code, sev, c in by_rule_rows:
        d = summary.setdefault(code, {"critical": 0, "warning": 0, "info": 0})
        d[sev] = int(c)

    from app.modules.arsenal.services.arsenal_analyzer import RULE_CATALOG
    return {
        "total": total,
        "summary_by_rule": summary,
        "catalog": RULE_CATALOG,
        "items": [
            {
                "id": r.id,
                "rule_code": r.rule_code,
                "severity": r.severity,
                "title": r.title,
                "details": r.details,
                "entity_type": r.entity_type,
                "entity_id": r.entity_id,
                "first_seen_at": r.first_seen_at,
                "last_seen_at": r.last_seen_at,
                "dismissed_at": r.dismissed_at,
                "dismiss_reason": r.dismiss_reason,
                "resolved_at": r.resolved_at,
            }
            for r in rows
        ],
    }


class DismissBody(BaseModel):
    reason: Optional[str] = None


@router.post("/analyzer/anomalies/{anomaly_id}/dismiss")
async def dismiss_anomaly(
    anomaly_id: int,
    body: Optional[DismissBody] = None,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    """Пометить аномалию как разобранную / false-positive. Она перестаёт
    светиться в виджете. Если правило снова сработает с теми же параметрами —
    запись будет обновлена (last_seen_at), но dismissed_at останется."""
    _require_admin(current_user)
    flag = await db.get(ArsenalAnomalyFlag, anomaly_id)
    if not flag:
        raise HTTPException(404, "Аномалия не найдена")
    flag.dismissed_at = datetime.utcnow()
    flag.dismissed_by_id = current_user.id
    flag.dismiss_reason = (body.reason if body else None)

    from app.modules.arsenal.services.audit import write_arsenal_audit
    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="dismiss_anomaly", entity_type="anomaly_flag", entity_id=anomaly_id,
        details={"rule_code": flag.rule_code, "reason": flag.dismiss_reason},
    )
    await db.commit()
    return {"status": "dismissed"}


@router.post("/analyzer/run")
async def run_analyzer_now(
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    """Запустить все правила вручную. Полезно после большого импорта
    или правки данных, когда не хочется ждать Celery-слот."""
    _require_admin(current_user)

    # Правила написаны под sync-сессию (DB-запросы без await) — берём её
    # из sync_db_session на арсенал. Вся работа — в отдельном блоке, не
    # блокирующем event loop надолго.
    from app.core.database import arsenal_engine
    from sqlalchemy.orm import Session

    def _run():
        from app.modules.arsenal.services.arsenal_analyzer import run_arsenal_analyzer
        with Session(arsenal_engine.sync_engine) as s:
            res = run_arsenal_analyzer(s)
            s.commit()
            return res

    import asyncio
    results = await asyncio.to_thread(_run)

    from app.modules.arsenal.services.audit import write_arsenal_audit
    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="run_analyzer", entity_type="analyzer", details=results,
    )
    await db.commit()
    return {"status": "ok", "findings": results}
