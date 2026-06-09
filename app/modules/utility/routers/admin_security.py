"""Сводка безопасности: приём отчётов сканеров из CI + выдача в админ-вкладку.

Единая точка интеграции — CI (GitHub Actions), а НЕ прод-бэкенд: CI и так
гоняет SonarQube/ZAP/Trivy/Bandit и видит их, а прод-ВМ может не видеть
SonarQube-сервер (как было с 1С/ГИС в локалке). Поэтому CI после сканов
POST'ит сводки сюда (токеном), бэкенд складывает в SystemSetting, а админ-
вкладка их показывает. Канон находок остаётся в GitHub Security / SonarQube UI —
тут обзор для тех, кто живёт в админке.

Хранилище: SystemSetting['security_findings'] = JSON {tool: {...}} (как
gisgmp_findings). Без отдельной таблицы/миграции.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import RoleChecker
from app.modules.utility.models import SystemSetting

router = APIRouter(prefix="/api/admin/security", tags=["Admin Security"])
logger = logging.getLogger(__name__)

# Сводка дыр — чувствительная страница: только admin (не accountant/financier).
allow_security = RoleChecker(["admin"])

SECURITY_FINDINGS_KEY = "security_findings"


def _check_security_token(authorization: Optional[str]) -> None:
    """Сверяет Bearer-токен с SECURITY_SYNC_TOKEN (constant-time). Зеркалит
    _check_gisgmp_token: машинный канал CI → платформа, без JWT."""
    expected = (settings.SECURITY_SYNC_TOKEN or "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Приём сводок безопасности не настроен: задайте SECURITY_SYNC_TOKEN в .env",
        )
    token = ""
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
    if not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Неверный токен сводки безопасности")


class SecurityReportIn(BaseModel):
    """Сводка одного сканера (один POST = один инструмент)."""
    tool: str = Field(..., description="Ключ инструмента: zap|sonarqube|trivy|bandit|...")
    label: Optional[str] = Field(None, description="Человекочитаемое имя, напр. «OWASP ZAP»")
    status: Optional[str] = Field(None, description="pass|fail|OK|ERROR|None")
    run_url: Optional[str] = Field(None, description="Ссылка на прогон CI / Sonar / Security tab")
    counts: dict[str, int] = Field(default_factory=dict, description="{severity|metric: число}")
    top: list[dict] = Field(default_factory=list, description="Топ-находки [{sev,title,where}]")
    generated_at: Optional[str] = Field(None, description="ISO-время скана (из CI)")


@router.post("/report")
async def ingest_security_report(
    payload: SecurityReportIn,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """CI POST'ит сюда сводку одного инструмента (токеном SECURITY_SYNC_TOKEN).
    Мёржится в SystemSetting['security_findings'][tool], перетирая прошлую
    сводку этого инструмента. top ограничиваем (не раздуваем хранилище)."""
    _check_security_token(authorization)

    entry = payload.model_dump()
    # top ограничиваем — это обзор, не полная копия (она в Sonar/GitHub).
    entry["top"] = (payload.top or [])[:50]
    entry["received_at"] = datetime.now(timezone.utc).isoformat()

    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == SECURITY_FINDINGS_KEY)
    )).scalar_one_or_none()
    try:
        data = json.loads(row.value) if row and row.value else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, TypeError):
        data = {}

    data[payload.tool] = entry

    if row:
        row.value = json.dumps(data, ensure_ascii=False)
    else:
        db.add(SystemSetting(
            key=SECURITY_FINDINGS_KEY,
            value=json.dumps(data, ensure_ascii=False),
            description="Сводки сканеров безопасности из CI (Sonar/ZAP/Trivy/Bandit)",
        ))
    await db.commit()
    logger.info("[security] принят отчёт tool=%s counts=%s", payload.tool, payload.counts)
    return {"status": "ok", "tool": payload.tool, "tools_total": len(data)}


@router.get("/findings")
async def get_security_findings(
    current_user=Depends(allow_security),
    db: AsyncSession = Depends(get_db),
):
    """Выдаёт сводки всех инструментов для админ-вкладки (только admin).
    Считает агрегаты по severity поверх counts каждого инструмента."""
    row = (await db.execute(
        select(SystemSetting).where(SystemSetting.key == SECURITY_FINDINGS_KEY)
    )).scalar_one_or_none()
    try:
        data = json.loads(row.value) if row and row.value else {}
        if not isinstance(data, dict):
            data = {}
    except (ValueError, TypeError):
        data = {}

    # Агрегат по «опасным» severity (то, что есть у большинства инструментов).
    totals = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    for entry in data.values():
        counts = (entry or {}).get("counts") or {}
        for sev in totals:
            try:
                totals[sev] += int(counts.get(sev, 0) or 0)
            except (ValueError, TypeError):
                pass

    return {
        "configured": bool((settings.SECURITY_SYNC_TOKEN or "").strip()),
        "tools": data,
        "totals": totals,
    }
