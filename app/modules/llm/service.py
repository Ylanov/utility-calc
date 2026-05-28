"""service.py — высокоуровневая обёртка ask() для LLM (L3).

Каждый вызов:
  1. Проверяет enabled + не превышен ли disabled_until.
  2. Проверяет дневной бюджет (сумма cost_rub за сегодня).
  3. Делает запрос через client.LLMClient.
  4. Считает приблизительную стоимость по тарифу провайдера.
  5. Сохраняет audit-запись в llm_calls.
  6. При превышении бюджета — disabled_until = next midnight UTC.

Цены ниже — ориентировочные на май 2026. Админ может скорректировать
prices_per_1k_tokens в коде или в analyzer_settings.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_utils import utcnow
from app.modules.llm.client import LLMClientError, LLMResponse, make_client
from app.modules.llm.crypto import decrypt_token, is_crypto_ready
from app.modules.utility.models import LLMCall, LLMSetting


logger = logging.getLogger(__name__)


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
    """Сколько уже потрачено за сегодня по UTC."""
    today_start = datetime.combine(utcnow().date(), dtime.min)
    spent = (await db.execute(
        select(func.coalesce(func.sum(LLMCall.cost_rub), 0))
        .where(LLMCall.occurred_at >= today_start)
        .where(LLMCall.success.is_(True))
    )).scalar_one()
    return Decimal(spent or 0)


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

    # Бюджет.
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

    client = make_client(s.provider, token, base_url=s.base_url)

    prompt_chars = sum(len(m.get("content", "")) for m in messages)

    started = time.monotonic()
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

    # Считаем стоимость.
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

    # Проверяем после этого вызова — превысили ли бюджет?
    disabled_after = False
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


__all__ = ["ask", "is_available", "get_settings", "LLMServiceResult"]
