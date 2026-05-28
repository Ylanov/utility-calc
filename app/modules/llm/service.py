"""service.py — высокоуровневая обёртка ask() для LLM (L3 + L8).

Каждый вызов:
  1. Проверяет enabled + не превышен ли disabled_until.
  2. Бюджет:
     - если monthly_budget_tokens > 0 → Freemium-режим: считаем токены
       за месяц (с даты monthly_period_start);
     - иначе → старый рубль/день (cost_rub за сегодня vs daily_budget_rub).
  3. Берёт Redis-lock (1 поток для Freemium — иначе 429 от GigaChat).
  4. Делает запрос через client.LLMClient.
  5. Считает приблизительную стоимость + использованные токены.
  6. Сохраняет audit-запись в llm_calls.
  7. При превышении бюджета — disabled_until до конца периода.

Цены ниже — ориентировочные на май 2026.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_utils import utcnow
from app.modules.llm.client import LLMClientError, LLMResponse, make_client
from app.modules.llm.crypto import decrypt_token, is_crypto_ready
from app.modules.utility.models import LLMCall, LLMSetting


logger = logging.getLogger(__name__)

# Redis-lock для соблюдения «1 параллельный поток» (Freemium-ограничение
# GigaChat). Ключ shared между web и celery — критично, чтобы admin'ский
# тест-запрос не пересекался с фоновым analyze_errors.
_REDIS_LOCK_KEY = "llm:single_thread"
_REDIS_LOCK_TIMEOUT_SEC = 90  # длиннее самого долгого chat-запроса (60с)

_redis_client = None


def _get_redis():
    """Lazy-init redis-клиента. Возвращает None при сбое."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        from redis.asyncio import Redis
        from app.core.config import settings as _cfg
        _redis_client = Redis.from_url(_cfg.REDIS_URL, decode_responses=True)
        return _redis_client
    except Exception as e:
        logger.warning("[llm.service] Redis init failed: %s", e)
        return None


async def _try_acquire_lock() -> bool:
    """Пытается взять lock. True = взяли, False = занят."""
    r = _get_redis()
    if r is None:
        # Если Redis недоступен — не блокируем, но логируем (production-risk).
        logger.warning("[llm.service] Redis unavailable — running WITHOUT lock")
        return True
    try:
        return bool(await r.set(_REDIS_LOCK_KEY, "1",
                                 nx=True, ex=_REDIS_LOCK_TIMEOUT_SEC))
    except Exception as e:
        logger.warning("[llm.service] Lock acquire failed: %s", e)
        return True  # graceful: пускаем без lock'а


async def _release_lock() -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        await r.delete(_REDIS_LOCK_KEY)
    except Exception:
        pass


# Цены за 1000 токенов в рублях (ориентир, май 2026).
# Если провайдер вернул usage — берём оттуда. Если нет — оцениваем по
# (prompt_chars + response_chars) / 3 как грубое приближение токенов.
PRICES_PER_1K_TOKENS = {
    # GigaChat Lite — самая дешёвая модель Сбера.
    "GigaChat":       {"prompt_rub": Decimal("0.20"), "response_rub": Decimal("0.40")},
    "GigaChat-Pro":   {"prompt_rub": Decimal("1.50"), "response_rub": Decimal("1.80")},
    "GigaChat-Max":   {"prompt_rub": Decimal("1.95"), "response_rub": Decimal("2.30")},
    # Локальные модели — 0 (только электричество).
    "local":          {"prompt_rub": Decimal("0"), "response_rub": Decimal("0")},
}


@dataclass
class LLMServiceResult:
    """Результат вызова service.ask()."""
    ok: bool
    text: Optional[str] = None
    error: Optional[str] = None
    cost_rub: Optional[Decimal] = None
    latency_ms: Optional[int] = None
    disabled_after: bool = False  # True если этот вызов исчерпал бюджет


class LLMDisabled(Exception):
    """Провайдер выключен (enabled=False) или нет токена / нет crypto-key."""


async def get_settings(db: AsyncSession) -> Optional[LLMSetting]:
    """Возвращает singleton LLMSetting (id=1) или None."""
    return await db.get(LLMSetting, 1)


async def is_available(db: AsyncSession) -> tuple[bool, Optional[str]]:
    """Проверяет можно ли сейчас звать LLM. Возвращает (ok, reason_if_no)."""
    s = await get_settings(db)
    if not s:
        return False, "LLM settings not initialized"
    if not s.enabled:
        return False, "LLM disabled by admin"
    if not s.token_encrypted:
        return False, "LLM token not set"
    if not is_crypto_ready():
        return False, "LLM_SECRET_KEY env not configured"
    if s.disabled_until and s.disabled_until > utcnow():
        return False, f"LLM disabled until {s.disabled_until} ({s.disabled_reason})"
    return True, None


async def _today_spent_rub(db: AsyncSession) -> Decimal:
    """Сколько уже потрачено за сегодня по UTC (рубль-режим)."""
    today_start = datetime.combine(utcnow().date(), dtime.min)
    spent = (await db.execute(
        select(func.coalesce(func.sum(LLMCall.cost_rub), 0))
        .where(LLMCall.occurred_at >= today_start)
        .where(LLMCall.success.is_(True))
    )).scalar_one()
    return Decimal(spent or 0)


async def _month_spent_tokens(db: AsyncSession, period_start: date) -> int:
    """Сколько токенов потрачено с даты period_start (Freemium-режим).

    Считаем сумму (prompt_tokens + response_tokens) по успешным вызовам.
    """
    pstart = datetime.combine(period_start, dtime.min)
    spent = (await db.execute(
        select(
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0)
            + func.coalesce(func.sum(LLMCall.response_tokens), 0)
        )
        .where(LLMCall.occurred_at >= pstart)
        .where(LLMCall.success.is_(True))
    )).scalar_one()
    return int(spent or 0)


def _default_period_start() -> date:
    """1-е число текущего месяца — fallback если monthly_period_start не задан."""
    today = utcnow().date()
    return today.replace(day=1)


def _estimate_cost(model_name: str, prompt_tokens: int, response_tokens: int) -> Decimal:
    """Возвращает оценку стоимости в рублях."""
    prices = PRICES_PER_1K_TOKENS.get(model_name) or PRICES_PER_1K_TOKENS["GigaChat"]
    cost = (
        Decimal(prompt_tokens) * prices["prompt_rub"] / Decimal(1000)
        + Decimal(response_tokens) * prices["response_rub"] / Decimal(1000)
    )
    return cost.quantize(Decimal("0.0001"))


def _approx_tokens_from_chars(chars: int) -> int:
    """Грубое приближение: ~3 chars/token для русского текста."""
    return max(1, chars // 3)


def _next_midnight_utc() -> datetime:
    today = utcnow().date()
    return datetime.combine(today + timedelta(days=1), dtime.min)


async def ask(
    db: AsyncSession,
    messages: list[dict],
    *,
    purpose: str,
    related_type: Optional[str] = None,
    related_id: Optional[int] = None,
    temperature: float = 0.3,
    max_tokens: int = 1500,
    timeout: int = 60,
) -> LLMServiceResult:
    """Главная функция: вызывает LLM с audit и budget-check.

    Не raise'ит при typical-ошибках — возвращает LLMServiceResult.ok=False.
    Raise только при программных ошибках (передан bad messages, например).
    """
    s = await get_settings(db)
    if not s or not s.enabled or not s.token_encrypted:
        return LLMServiceResult(ok=False, error="LLM not configured/enabled")

    if not is_crypto_ready():
        return LLMServiceResult(ok=False, error="LLM_SECRET_KEY env not configured")

    if s.disabled_until and s.disabled_until > utcnow():
        return LLMServiceResult(
            ok=False,
            error=f"LLM disabled until {s.disabled_until} ({s.disabled_reason})",
        )

    # ─────── БЮДЖЕТ: 2 режима ───────
    # Если monthly_budget_tokens > 0 → Freemium (приоритетный).
    # Иначе → старый рубль/день.
    is_freemium = (s.monthly_budget_tokens or 0) > 0
    if is_freemium:
        period_start = s.monthly_period_start or _default_period_start()
        spent_tokens = await _month_spent_tokens(db, period_start)
        token_budget = int(s.monthly_budget_tokens)
        if spent_tokens >= token_budget:
            # Блокируем до следующего месяца (приблизительно).
            s.disabled_until = _next_month_start_utc()
            s.disabled_reason = (
                f"monthly_token_budget_exceeded: {spent_tokens}/{token_budget} токенов "
                f"(с {period_start})"
            )
            await db.commit()
            return LLMServiceResult(
                ok=False,
                error=(
                    f"Месячный токен-бюджет исчерпан: {spent_tokens}/{token_budget}. "
                    f"Сбер обновит лимиты в начале следующего месяца, либо купи доп. пакет."
                ),
                disabled_after=True,
            )
    else:
        spent_today = await _today_spent_rub(db)
        budget = Decimal(s.daily_budget_rub or 50)
        if spent_today >= budget:
            s.disabled_until = _next_midnight_utc()
            s.disabled_reason = f"daily_budget_exceeded: {spent_today}/{budget} ₽"
            await db.commit()
            return LLMServiceResult(
                ok=False,
                error=f"Daily budget exceeded: {spent_today} >= {budget} ₽",
                disabled_after=True,
            )

    token = decrypt_token(s.token_encrypted)
    if not token:
        return LLMServiceResult(ok=False, error="Failed to decrypt token (wrong key?)")

    # ─────── REDIS LOCK: 1 параллельный поток ───────
    if not await _try_acquire_lock():
        return LLMServiceResult(
            ok=False,
            error=(
                "LLM сейчас занят другим запросом (Freemium = 1 поток). "
                "Попробуй через 30-60 секунд."
            ),
        )

    client = make_client(s.provider, token, base_url=s.base_url)
    prompt_chars = sum(len(m.get("content", "")) for m in messages)
    started = time.monotonic()

    try:
        try:
            resp: LLMResponse = await client.chat(
                messages,
                model=s.model_name,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
        except LLMClientError as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # Сохраняем failed-вызов (не учитываем в бюджете success-only).
            call = LLMCall(
                purpose=purpose, provider=s.provider, model_name=s.model_name,
                prompt_chars=prompt_chars, response_chars=None,
                success=False, error=str(e)[:5000],
                latency_ms=elapsed_ms,
                related_type=related_type, related_id=related_id,
            )
            db.add(call)
            await db.commit()
            return LLMServiceResult(ok=False, error=str(e), latency_ms=elapsed_ms)
    finally:
        await _release_lock()

    # Считаем токены и стоимость.
    prompt_tokens = resp.prompt_tokens or _approx_tokens_from_chars(prompt_chars)
    response_tokens = resp.response_tokens or _approx_tokens_from_chars(len(resp.text))
    cost = _estimate_cost(s.model_name, prompt_tokens, response_tokens)

    call = LLMCall(
        purpose=purpose, provider=s.provider, model_name=s.model_name,
        prompt_chars=prompt_chars, response_chars=len(resp.text),
        prompt_tokens=prompt_tokens, response_tokens=response_tokens,
        cost_rub=cost, latency_ms=elapsed_ms,
        success=True, error=None,
        related_type=related_type, related_id=related_id,
    )
    db.add(call)

    # Проверяем превышение после этого вызова — для авто-блокировки.
    disabled_after = False
    used_tokens_this = prompt_tokens + response_tokens
    if is_freemium:
        if (spent_tokens + used_tokens_this) >= token_budget:
            s.disabled_until = _next_month_start_utc()
            s.disabled_reason = (
                f"monthly_token_budget_exceeded_after_call: "
                f"{spent_tokens + used_tokens_this}/{token_budget} токенов"
            )
            disabled_after = True
    else:
        if (spent_today + cost) >= budget:
            s.disabled_until = _next_midnight_utc()
            s.disabled_reason = (
                f"daily_budget_exceeded_after_call: {spent_today + cost}/{budget} ₽"
            )
            disabled_after = True

    await db.commit()

    return LLMServiceResult(
        ok=True, text=resp.text, cost_rub=cost,
        latency_ms=elapsed_ms, disabled_after=disabled_after,
    )


def _next_month_start_utc() -> datetime:
    """1-е число СЛЕДУЮЩЕГО месяца, 00:00 UTC."""
    today = utcnow().date()
    if today.month == 12:
        return datetime.combine(date(today.year + 1, 1, 1), dtime.min)
    return datetime.combine(date(today.year, today.month + 1, 1), dtime.min)


__all__ = ["ask", "is_available", "get_settings", "LLMServiceResult"]
