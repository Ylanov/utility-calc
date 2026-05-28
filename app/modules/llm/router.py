"""router.py — админские эндпоинты ИИ-помощника (L4).

  GET    /api/admin/llm/settings  — текущий конфиг (БЕЗ открытого токена).
  PUT    /api/admin/llm/settings  — обновить (provider/model/enabled/budget).
  POST   /api/admin/llm/token     — сохранить новый токен (Fernet-шифрование).
  DELETE /api/admin/llm/token     — стереть токен.
  POST   /api/admin/llm/test      — тестовый запрос (test-prompt) с замером
                                    latency/cost.
  GET    /api/admin/llm/usage     — статистика звонков (по дням/purposes).
  GET    /api/admin/llm/calls     — последние N llm_calls (для debug).
  POST   /api/admin/llm/reset-disabled — сбросить disabled_until вручную.

  GET    /api/admin/llm/crypto-status — есть ли env LLM_SECRET_KEY.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.core.time_utils import utcnow
from app.modules.llm import service as llm_service
from app.modules.llm.crypto import encrypt_token, is_crypto_ready
from app.modules.llm.prompts import build_test_prompt
from app.modules.utility.models import LLMCall, User


router = APIRouter(prefix="/api/admin/llm", tags=["Admin LLM"])


def _require_admin(user: User) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Только admin (не accountant)")


# =====================================================================
# Schemas
# =====================================================================
class SettingsUpdate(BaseModel):
    provider: Optional[str] = Field(None, pattern="^(gigachat_lite|gigachat_pro|gigachat_max|local_vllm|disabled)$")
    model_name: Optional[str] = Field(None, max_length=64)
    enabled: Optional[bool] = None
    daily_budget_rub: Optional[float] = Field(None, ge=0, le=100000)
    # L8 Freemium: лимит токенов в месяц. Если > 0 — приоритетный
    # (рубль-бюджет игнорируется). По дефолту 0.
    monthly_budget_tokens: Optional[int] = Field(None, ge=0, le=100_000_000)
    monthly_period_start: Optional[str] = Field(
        None, description="ISO-дата начала текущего периода (YYYY-MM-DD)",
    )
    base_url: Optional[str] = Field(None, max_length=256)


class TokenSet(BaseModel):
    token: str = Field(..., min_length=10, max_length=1000,
                       description="Authorization Key из личного кабинета Сбер "
                                   "(base64(client_id:client_secret))")


# =====================================================================
# Settings
# =====================================================================
@router.get("/crypto-status")
async def crypto_status(current_user: User = Depends(get_current_user)):
    """Проверка что env LLM_SECRET_KEY настроен (для UI красной плашки)."""
    _require_admin(current_user)
    return {
        "crypto_ready": is_crypto_ready(),
        "hint": (
            "Добавьте в .env переменную LLM_SECRET_KEY=<32 байта base64>. "
            "Сгенерировать: `python -c \"from app.modules.llm.crypto import "
            "generate_new_key; print(generate_new_key())\"`. После добавления "
            "перезапустите web и worker-контейнеры."
        ),
    }


@router.get("/settings")
async def get_settings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Текущий конфиг LLM. Токен НЕ возвращается — только последние 4 символа
    шифротекста как маркер «настроен / не настроен»."""
    _require_admin(current_user)
    s = await llm_service.get_settings(db)
    if not s:
        raise HTTPException(404, "LLM settings missing (миграция не применена?)")
    token_hint = None
    if s.token_encrypted:
        token_hint = "****" + s.token_encrypted[-4:]
    return {
        "id": s.id,
        "provider": s.provider,
        "model_name": s.model_name,
        "token_hint": token_hint,
        "token_set": bool(s.token_encrypted),
        "base_url": s.base_url,
        "enabled": s.enabled,
        "daily_budget_rub": float(s.daily_budget_rub or 0),
        # L8 Freemium:
        "monthly_budget_tokens": int(s.monthly_budget_tokens or 0),
        "monthly_period_start": s.monthly_period_start.isoformat() if s.monthly_period_start else None,
        "budget_mode": "tokens" if (s.monthly_budget_tokens or 0) > 0 else "rub",
        "disabled_until": s.disabled_until.isoformat() if s.disabled_until else None,
        "disabled_reason": s.disabled_reason,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "crypto_ready": is_crypto_ready(),
    }


@router.put("/settings")
async def update_settings(
    body: SettingsUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    s = await llm_service.get_settings(db)
    if not s:
        raise HTTPException(404, "LLM settings missing")
    if body.provider is not None: s.provider = body.provider
    if body.model_name is not None: s.model_name = body.model_name
    if body.enabled is not None: s.enabled = body.enabled
    if body.daily_budget_rub is not None: s.daily_budget_rub = body.daily_budget_rub
    if body.monthly_budget_tokens is not None:
        s.monthly_budget_tokens = body.monthly_budget_tokens
    if body.monthly_period_start is not None:
        if body.monthly_period_start == "":
            s.monthly_period_start = None
        else:
            from datetime import date as _date
            try:
                s.monthly_period_start = _date.fromisoformat(body.monthly_period_start)
            except ValueError:
                raise HTTPException(400, "monthly_period_start: ожидаю YYYY-MM-DD")
    if body.base_url is not None: s.base_url = body.base_url or None
    s.updated_at = utcnow()
    s.updated_by_id = current_user.id
    await db.commit()
    return {"status": "ok"}


@router.post("/token")
async def set_token(
    body: TokenSet,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    if not is_crypto_ready():
        raise HTTPException(
            status_code=400,
            detail="LLM_SECRET_KEY env не настроен — нельзя сохранить токен безопасно. "
                   "См. /api/admin/llm/crypto-status для инструкции.",
        )
    s = await llm_service.get_settings(db)
    if not s:
        raise HTTPException(404, "LLM settings missing")
    enc = encrypt_token(body.token.strip())
    if not enc:
        raise HTTPException(500, "Шифрование не удалось — проверьте LLM_SECRET_KEY")
    s.token_encrypted = enc
    s.updated_at = utcnow()
    s.updated_by_id = current_user.id
    # Сбрасываем disabled_until — новый токен значит «попробуй снова».
    s.disabled_until = None
    s.disabled_reason = None
    await db.commit()
    return {"status": "ok", "token_hint": "****" + enc[-4:]}


@router.delete("/token")
async def delete_token(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(current_user)
    s = await llm_service.get_settings(db)
    if not s:
        raise HTTPException(404, "LLM settings missing")
    s.token_encrypted = None
    s.enabled = False
    s.updated_at = utcnow()
    s.updated_by_id = current_user.id
    await db.commit()
    return {"status": "ok"}


# =====================================================================
# TEST endpoint
# =====================================================================
@router.post("/test")
async def test_llm(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Тест-запрос: пингует провайдера простым промптом, возвращает
    ответ + latency + cost. Используется кнопкой 'Тест' в UI."""
    _require_admin(current_user)
    res = await llm_service.ask(
        db,
        build_test_prompt(),
        purpose="test",
        temperature=0.1,
        max_tokens=200,
    )
    return {
        "ok": res.ok,
        "text": res.text,
        "error": res.error,
        "cost_rub": float(res.cost_rub) if res.cost_rub else None,
        "latency_ms": res.latency_ms,
        "disabled_after": res.disabled_after,
    }


# =====================================================================
# USAGE statistics
# =====================================================================
@router.get("/usage")
async def usage(
    days: int = 30,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Статистика: сколько вызовов / денег / средний latency за N дней,
    разбивка по дням и по purposes."""
    _require_admin(current_user)
    since = utcnow() - timedelta(days=days)

    total_calls = (await db.execute(
        select(func.count(LLMCall.id)).where(LLMCall.occurred_at >= since)
    )).scalar_one()
    total_success = (await db.execute(
        select(func.count(LLMCall.id)).where(
            LLMCall.occurred_at >= since, LLMCall.success.is_(True))
    )).scalar_one()
    total_cost = (await db.execute(
        select(func.coalesce(func.sum(LLMCall.cost_rub), 0)).where(
            LLMCall.occurred_at >= since, LLMCall.success.is_(True))
    )).scalar_one()

    # По purposes.
    by_purpose = (await db.execute(
        select(
            LLMCall.purpose,
            func.count(LLMCall.id),
            func.coalesce(func.sum(LLMCall.cost_rub), 0),
        ).where(LLMCall.occurred_at >= since).group_by(LLMCall.purpose)
    )).all()

    # Сегодня.
    today_start = datetime.combine(utcnow().date(), datetime.min.time())
    today_cost = (await db.execute(
        select(func.coalesce(func.sum(LLMCall.cost_rub), 0)).where(
            LLMCall.occurred_at >= today_start, LLMCall.success.is_(True))
    )).scalar_one()
    today_calls = (await db.execute(
        select(func.count(LLMCall.id)).where(
            LLMCall.occurred_at >= today_start)
    )).scalar_one()

    s = await llm_service.get_settings(db)
    budget = float(s.daily_budget_rub) if s else 50.0

    # L8: токен-статистика для Freemium.
    token_stats = None
    if s and (s.monthly_budget_tokens or 0) > 0:
        from datetime import date as _date
        pstart = s.monthly_period_start or _date.today().replace(day=1)
        spent_tokens = await llm_service._month_spent_tokens(db, pstart)
        budget_tokens = int(s.monthly_budget_tokens)
        token_stats = {
            "period_start": pstart.isoformat(),
            "tokens_used": spent_tokens,
            "tokens_budget": budget_tokens,
            "tokens_remaining": max(0, budget_tokens - spent_tokens),
            "used_pct": (spent_tokens / budget_tokens * 100) if budget_tokens > 0 else 0,
        }

    return {
        "period_days": days,
        "total_calls": total_calls,
        "total_success": total_success,
        "total_failed": total_calls - total_success,
        "total_cost_rub": float(total_cost),
        "today_cost_rub": float(today_cost),
        "today_calls": today_calls,
        "today_budget_rub": budget,
        "today_used_pct": (float(today_cost) / budget * 100) if budget > 0 else 0,
        "budget_mode": "tokens" if token_stats else "rub",
        "token_stats": token_stats,
        "by_purpose": [
            {"purpose": p, "calls": int(cnt), "cost_rub": float(cost)}
            for p, cnt, cost in by_purpose
        ],
    }


@router.get("/calls")
async def list_calls(
    limit: int = 100,
    purpose: Optional[str] = None,
    success_only: Optional[bool] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Последние N llm_calls (для debug)."""
    _require_admin(current_user)
    q = select(LLMCall).order_by(desc(LLMCall.occurred_at)).limit(min(limit, 500))
    if purpose:
        q = q.where(LLMCall.purpose == purpose)
    if success_only is not None:
        q = q.where(LLMCall.success.is_(success_only))
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": r.id,
            "occurred_at": r.occurred_at.isoformat(),
            "purpose": r.purpose,
            "provider": r.provider,
            "model_name": r.model_name,
            "prompt_chars": r.prompt_chars,
            "response_chars": r.response_chars,
            "prompt_tokens": r.prompt_tokens,
            "response_tokens": r.response_tokens,
            "cost_rub": float(r.cost_rub) if r.cost_rub else None,
            "latency_ms": r.latency_ms,
            "success": r.success,
            "error_short": (r.error or "")[:300] if r.error else None,
            "related_type": r.related_type,
            "related_id": r.related_id,
        }
        for r in rows
    ]


@router.post("/reset-disabled")
async def reset_disabled(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Снять авто-блокировку (например после пополнения баланса)."""
    _require_admin(current_user)
    s = await llm_service.get_settings(db)
    if not s:
        raise HTTPException(404, "LLM settings missing")
    s.disabled_until = None
    s.disabled_reason = None
    await db.commit()
    return {"status": "ok"}


# =====================================================================
# L6: AI-резюме жильца
# =====================================================================
# Простой in-memory кеш: user_id → (text, generated_at), TTL=1 час.
# Чтобы повторное нажатие на ту же карточку не палило бюджет.
_USER_SUMMARY_CACHE: dict[int, tuple[str, datetime]] = {}
_USER_SUMMARY_TTL = timedelta(hours=1)


@router.get("/user-summary/{user_id}")
async def user_summary(
    user_id: int,
    force_refresh: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает AI-резюме жильца. Кеш TTL=1 час чтобы не палить бюджет.

    Доступно admin И accountant (бухгалтер часто нужен).
    """
    if current_user.role not in ("admin", "accountant"):
        raise HTTPException(403, "Только admin/accountant")

    # Кеш.
    cached = _USER_SUMMARY_CACHE.get(user_id)
    if cached and not force_refresh:
        text, ts = cached
        if (utcnow() - ts) < _USER_SUMMARY_TTL:
            return {
                "ok": True,
                "text": text,
                "cached": True,
                "generated_at": ts.isoformat(),
            }

    # Подгружаем контекст жильца.
    from app.modules.utility.models import (
        GSheetsImportRow, MeterReading, Room, User as _User,
    )
    from app.modules.llm.prompts import build_user_summary_prompt

    user_row = await db.get(_User, user_id)
    if not user_row:
        raise HTTPException(404, "Жилец не найден")

    room = await db.get(Room, user_row.room_id) if user_row.room_id else None

    readings = (await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == user_id)
        .order_by(desc(MeterReading.created_at))
        .limit(12)
    )).scalars().all()

    gs_rows = (await db.execute(
        select(GSheetsImportRow)
        .where(GSheetsImportRow.matched_user_id == user_id)
        .order_by(desc(GSheetsImportRow.sheet_timestamp))
        .limit(6)
    )).scalars().all()

    context = {
        "user": {
            "id": user_row.id,
            "username": user_row.username,
            "residents_count": user_row.residents_count,
            "billing_mode": getattr(user_row, "billing_mode", None),
            "resident_type": getattr(user_row, "resident_type", None),
            "tariff_id": user_row.tariff_id,
            "is_deleted": user_row.is_deleted,
        },
        "room": {
            "id": room.id if room else None,
            "place_type": room.place_type if room else None,
            "format_address": room.format_address if room else None,
            "apartment_area": float(room.apartment_area) if room and room.apartment_area else None,
            "is_singles_apartment": getattr(room, "is_singles_apartment", None) if room else None,
        } if room else None,
        "recent_readings": [
            {
                "id": r.id,
                "period_id": r.period_id,
                "hot_water": float(r.hot_water) if r.hot_water is not None else None,
                "cold_water": float(r.cold_water) if r.cold_water is not None else None,
                "anomaly_flags": r.anomaly_flags,
                "total_cost": float(r.total_cost) if r.total_cost is not None else None,
                "is_approved": r.is_approved,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in readings
        ],
        "recent_gsheets_rows": [
            {
                "id": g.id,
                "sheet_timestamp": g.sheet_timestamp.isoformat() if g.sheet_timestamp else None,
                "raw_room_number": g.raw_room_number,
                "hot_water": float(g.hot_water) if g.hot_water is not None else None,
                "cold_water": float(g.cold_water) if g.cold_water is not None else None,
                "status": g.status,
                "conflict_reason": g.conflict_reason,
            }
            for g in gs_rows
        ],
    }

    messages = build_user_summary_prompt(context)
    res = await llm_service.ask(
        db, messages,
        purpose="user_summary",
        related_type="user", related_id=user_id,
        temperature=0.3,
        max_tokens=1200,
    )
    if not res.ok:
        return {"ok": False, "error": res.error,
                "cost_rub": float(res.cost_rub) if res.cost_rub else None}

    _USER_SUMMARY_CACHE[user_id] = (res.text, utcnow())
    return {
        "ok": True,
        "text": res.text,
        "cached": False,
        "cost_rub": float(res.cost_rub) if res.cost_rub else None,
        "latency_ms": res.latency_ms,
    }
