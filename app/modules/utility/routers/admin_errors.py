# app/modules/utility/routers/admin_errors.py
"""Админский журнал ошибок (E3-B, 28.05.2026).

Эндпоинты:
  GET    /api/admin/errors           — список с фильтрами
  GET    /api/admin/errors/stats     — счётчики (для бейджа на вкладке)
  GET    /api/admin/errors/{id}      — детали одной ошибки
  GET    /api/admin/errors/{id}/copy — markdown для копирования в Claude
  POST   /api/admin/errors/{id}/resolve   — пометить как решённую
  POST   /api/admin/errors/{id}/reopen    — снять resolved
  DELETE /api/admin/errors/{id}      — удалить (для совсем мусорных)

  POST   /api/errors/frontend        — публичный (без auth), но с rate-limit:
                                       клиентский JS присылает window.onerror
                                       и unhandledrejection-события.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.time_utils import utcnow
from app.modules.utility.models import ErrorLog, User


router = APIRouter(tags=["Admin Errors"])


def _require_admin(user: User) -> None:
    if user.role not in ("admin", "accountant"):
        raise HTTPException(status_code=403, detail="Доступ запрещён")


# =====================================================================
# Schemas
# =====================================================================
class ResolveBody(BaseModel):
    notes: Optional[str] = Field(None, max_length=2000)


class FrontendErrorBody(BaseModel):
    """JS-ошибки от клиента (без auth — клиент может быть на login-странице).

    Поля повторяют сигнатуру window.onerror + unhandledrejection.
    """
    message: str = Field(..., max_length=5000)
    source: Optional[str] = Field(None, max_length=500)  # URL JS-файла
    lineno: Optional[int] = None
    colno: Optional[int] = None
    stack: Optional[str] = Field(None, max_length=20000)
    url: Optional[str] = Field(None, max_length=500)     # window.location
    user_agent: Optional[str] = Field(None, max_length=500)
    request_id: Optional[str] = Field(None, max_length=64)


# =====================================================================
# LIST + STATS
# =====================================================================
@router.get("/api/admin/errors")
async def list_errors(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    source: Optional[str] = Query(None, pattern="^(backend|celery|frontend)$"),
    level: Optional[str] = Query(None, pattern="^(error|warning|info)$"),
    http_status: Optional[int] = Query(None),
    exc_type: Optional[str] = Query(None),
    path_contains: Optional[str] = Query(None),
    resolved: Optional[bool] = Query(None),
    since_hours: Optional[int] = Query(None, ge=1, le=720),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список ошибок с фильтрами и пагинацией."""
    _require_admin(current_user)

    q = select(ErrorLog)
    count_q = select(func.count(ErrorLog.id))

    conds = []
    if source:
        conds.append(ErrorLog.source == source)
    if level:
        conds.append(ErrorLog.level == level)
    if http_status:
        conds.append(ErrorLog.http_status == http_status)
    if exc_type:
        conds.append(ErrorLog.exc_type.ilike(f"%{exc_type}%"))
    if path_contains:
        conds.append(ErrorLog.http_path.ilike(f"%{path_contains}%"))
    if resolved is not None:
        conds.append(ErrorLog.resolved.is_(resolved))
    if since_hours:
        cutoff = utcnow() - timedelta(hours=since_hours)
        conds.append(ErrorLog.occurred_at >= cutoff)

    for c in conds:
        q = q.where(c)
        count_q = count_q.where(c)

    total = (await db.execute(count_q)).scalar_one()
    rows = (await db.execute(
        q.order_by(desc(ErrorLog.occurred_at))
        .offset((page - 1) * limit)
        .limit(limit)
    )).scalars().all()

    items = [
        {
            "id": r.id,
            "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            "source": r.source,
            "level": r.level,
            "http_method": r.http_method,
            "http_path": r.http_path,
            "http_status": r.http_status,
            "exc_type": r.exc_type,
            # Сообщение трим'аем — в списке не нужно всё.
            "exc_message_short": (r.exc_message or "")[:200],
            "user_id": r.user_id,
            "user_username": r.user_username,
            "request_id": r.request_id,
            "resolved": r.resolved,
            "copied_count": r.copied_count,
        }
        for r in rows
    ]
    return {"total": total, "page": page, "limit": limit, "items": items}


@router.get("/api/admin/errors/stats")
async def errors_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Счётчики для бейджа: всего unresolved, за последние 24ч, по source."""
    _require_admin(current_user)

    total_unresolved = (await db.execute(
        select(func.count(ErrorLog.id)).where(ErrorLog.resolved.is_(False))
    )).scalar_one()
    last_24h = (await db.execute(
        select(func.count(ErrorLog.id))
        .where(ErrorLog.occurred_at >= utcnow() - timedelta(hours=24))
    )).scalar_one()

    by_source = dict((await db.execute(
        select(ErrorLog.source, func.count(ErrorLog.id))
        .where(ErrorLog.resolved.is_(False))
        .group_by(ErrorLog.source)
    )).all())

    return {
        "total_unresolved": total_unresolved,
        "last_24h": last_24h,
        "by_source_unresolved": {k: v for k, v in by_source.items()},
    }


# =====================================================================
# DETAILS + COPY
# =====================================================================
@router.get("/api/admin/errors/{error_id}")
async def get_error(
    error_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Полная запись ошибки со всем контекстом."""
    _require_admin(current_user)
    r = await db.get(ErrorLog, error_id)
    if not r:
        raise HTTPException(404, "Ошибка не найдена")
    return _to_full_dict(r)


@router.get("/api/admin/errors/{error_id}/copy")
async def copy_error(
    error_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает markdown готовый для вставки в чат с AI-ассистентом.

    Инкрементирует copied_count — метрика «насколько эта ошибка часто
    репортилась админом».
    """
    _require_admin(current_user)
    r = await db.get(ErrorLog, error_id)
    if not r:
        raise HTTPException(404, "Ошибка не найдена")

    md = _format_for_claude(r)
    r.copied_count = (r.copied_count or 0) + 1
    await db.commit()
    return {"markdown": md, "copied_count": r.copied_count}


# =====================================================================
# RESOLVE / REOPEN / DELETE
# =====================================================================
@router.post("/api/admin/errors/{error_id}/resolve")
async def resolve_error(
    error_id: int,
    body: ResolveBody = Body(default_factory=ResolveBody),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    r = await db.get(ErrorLog, error_id)
    if not r:
        raise HTTPException(404, "Ошибка не найдена")
    r.resolved = True
    r.resolved_at = utcnow()
    r.resolved_by_id = current_user.id
    if body.notes:
        r.resolved_notes = body.notes
    await db.commit()
    return {"status": "ok", "resolved_at": r.resolved_at.isoformat()}


@router.post("/api/admin/errors/{error_id}/reopen")
async def reopen_error(
    error_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    r = await db.get(ErrorLog, error_id)
    if not r:
        raise HTTPException(404, "Ошибка не найдена")
    r.resolved = False
    r.resolved_at = None
    r.resolved_by_id = None
    await db.commit()
    return {"status": "ok"}


@router.delete("/api/admin/errors/{error_id}")
async def delete_error(
    error_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удалить запись (для очевидно мусорных)."""
    _require_admin(current_user)
    r = await db.get(ErrorLog, error_id)
    if not r:
        raise HTTPException(404, "Ошибка не найдена")
    await db.delete(r)
    await db.commit()
    return {"status": "ok"}


# =====================================================================
# FRONTEND endpoint (без auth — клиент может быть на login)
# =====================================================================
# Простой in-memory rate-limit: не больше N ошибок в минуту с одного IP.
# Цель — защита от спама от багнутого клиента с while(true) {throw}.
_FRONTEND_RATE_BUCKET: dict[str, list[datetime]] = {}
_FRONTEND_MAX_PER_MIN = 30


def _frontend_rate_limit_ok(client_ip: str) -> bool:
    now = utcnow()
    bucket = _FRONTEND_RATE_BUCKET.get(client_ip, [])
    # удаляем старше минуты
    cutoff = now - timedelta(minutes=1)
    bucket = [t for t in bucket if t >= cutoff]
    if len(bucket) >= _FRONTEND_MAX_PER_MIN:
        _FRONTEND_RATE_BUCKET[client_ip] = bucket
        return False
    bucket.append(now)
    _FRONTEND_RATE_BUCKET[client_ip] = bucket
    return True


@router.post("/api/errors/frontend")
async def log_frontend_error(
    payload: FrontendErrorBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """JS-клиент шлёт сюда window.onerror и unhandledrejection.

    Auth не требуется — клиент может быть на login-странице. Защита от
    флуда через простой rate-limit.
    """
    client_ip = request.client.host if request.client else "unknown"
    if not _frontend_rate_limit_ok(client_ip):
        # Тихо игнорируем, чтобы клиент не зацикливался на retry.
        return {"status": "rate_limited"}

    from app.core.error_logger import log_error
    await log_error(
        db,
        source="frontend",
        level="error",
        http_path=payload.url,
        exc_type=_extract_js_error_type(payload.message, payload.stack),
        exc_message=payload.message,
        traceback_str=payload.stack,
        request_id=payload.request_id,
        extra={
            "source_file": payload.source,
            "lineno": payload.lineno,
            "colno": payload.colno,
            "user_agent": payload.user_agent,
            "client_ip": client_ip,
        },
        run_investigation=False,
    )
    return {"status": "ok"}


def _extract_js_error_type(message: str, stack: Optional[str]) -> str:
    """Эвристика — пытаемся вытащить тип JS-ошибки (TypeError, ReferenceError)."""
    if stack:
        first_line = stack.split("\n", 1)[0].strip()
        if ":" in first_line:
            return first_line.split(":", 1)[0][:200]
    if message and ":" in message:
        return message.split(":", 1)[0][:200]
    return "JSError"


# =====================================================================
# Сериализация
# =====================================================================
def _to_full_dict(r: ErrorLog) -> dict:
    return {
        "id": r.id,
        "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
        "source": r.source,
        "level": r.level,
        "http_method": r.http_method,
        "http_path": r.http_path,
        "http_status": r.http_status,
        "exc_type": r.exc_type,
        "exc_message": r.exc_message,
        "traceback": r.traceback,
        "request_body": r.request_body,
        "user_id": r.user_id,
        "user_username": r.user_username,
        "request_id": r.request_id,
        "investigation": r.investigation,
        "extra": r.extra,
        "resolved": r.resolved,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
        "resolved_by_id": r.resolved_by_id,
        "resolved_notes": r.resolved_notes,
        "copied_count": r.copied_count,
    }


def _format_for_claude(r: ErrorLog) -> str:
    """Markdown-дамп для копирования в чат с AI-ассистентом.

    Структура:
      # Ошибка #<id> (<source>, <occurred_at>)
      ## Контекст запроса
      ## Исключение / traceback
      ## Что система знает (auto-investigation)
      ## Тело запроса
      ## Доп. метаданные
    """
    import json

    parts: list[str] = []

    parts.append(f"# Ошибка `error_log #{r.id}` ({r.source}, {r.occurred_at})\n")

    if r.http_method or r.http_path or r.http_status:
        parts.append("## HTTP-контекст")
        parts.append(f"- Метод: `{r.http_method or '?'}`")
        parts.append(f"- Путь: `{r.http_path or '?'}`")
        parts.append(f"- Статус ответа: `{r.http_status or '?'}`")
        if r.user_username or r.user_id:
            parts.append(f"- Пользователь: `{r.user_username or '?'}` (id={r.user_id})")
        if r.request_id:
            parts.append(f"- Request-ID: `{r.request_id}`")
        parts.append("")

    if r.exc_type or r.exc_message:
        parts.append("## Исключение")
        parts.append(f"**{r.exc_type or 'Unknown'}**: {r.exc_message or ''}")
        parts.append("")

    if r.traceback:
        parts.append("## Traceback")
        parts.append("```python")
        parts.append(r.traceback)
        parts.append("```")
        parts.append("")

    if r.investigation:
        parts.append("## Что система знает (auto-investigation)")
        parts.append("```json")
        parts.append(json.dumps(r.investigation, ensure_ascii=False, indent=2))
        parts.append("```")
        parts.append("")

    if r.request_body:
        parts.append("## Тело запроса")
        parts.append("```json")
        parts.append(json.dumps(r.request_body, ensure_ascii=False, indent=2))
        parts.append("```")
        parts.append("")

    if r.extra:
        parts.append("## Доп. метаданные")
        parts.append("```json")
        parts.append(json.dumps(r.extra, ensure_ascii=False, indent=2))
        parts.append("```")
        parts.append("")

    return "\n".join(parts)
