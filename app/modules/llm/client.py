"""client.py — REST-клиент LLM-провайдера (GigaChat сейчас).

Используется внутри service.py через async-обёртку. Не делает audit и
budget check (это слой выше). Чистый API-уровень.

GigaChat API:
  OAuth: POST https://ngw.devices.sberbank.ru:9443/api/v2/oauth
         Basic-auth с Authorization Key (выдаётся в личном кабинете Сбер).
         Body: scope=GIGACHAT_API_PERS (или _CORP / _B2B).
         Response: {"access_token": "...", "expires_at": <ms>}
  Chat:  POST https://gigachat.devices.sberbank.ru/api/v1/chat/completions
         Bearer-auth с access_token (TTL ~30 мин — кешируем в памяти).
         Body OpenAI-compat: {"model": "GigaChat", "messages": [...], ...}

SSL: GigaChat использует Russian Trusted Root CA. На пилоте используем
verify=False с warning в логе — для production желательно подложить
файл сертификата (см. https://developers.sber.ru/docs/ru/gigachat/certificates).
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


# Эндпоинты GigaChat.
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_CHAT_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"


@dataclass
class LLMResponse:
    """Унифицированный ответ от LLM-провайдера."""
    text: str
    prompt_tokens: Optional[int] = None
    response_tokens: Optional[int] = None
    raw: Optional[dict] = None


class LLMClientError(Exception):
    """Любая ошибка LLM-провайдера, не относящаяся к нашей логике."""


class LLMClient:
    """Интерфейс LLM-клиента. Конкретные реализации — GigaChatClient и др."""

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float = 0.3,
        max_tokens: int = 1500,
        timeout: int = 60,
    ) -> LLMResponse:
        raise NotImplementedError


# =====================================================================
# GigaChat
# =====================================================================

class GigaChatClient(LLMClient):
    """Клиент GigaChat. token_authorization_key — это base64-строка
    из личного кабинета Сбер (формат `Y2xpZW50X2lkOnNlY3JldA==` =
    base64(client_id:client_secret)).
    """

    def __init__(
        self,
        token_authorization_key: str,
        *,
        scope: str = "GIGACHAT_API_PERS",
        verify_ssl: bool = False,
        oauth_timeout: int = 15,
    ):
        self._auth_key = token_authorization_key
        self._scope = scope
        self._verify_ssl = verify_ssl
        self._oauth_timeout = oauth_timeout
        self._access_token: Optional[str] = None
        self._access_expires_at: float = 0.0  # epoch seconds

    async def _ensure_access_token(self) -> str:
        """Получает access_token или возвращает кешированный."""
        now = time.time()
        # Обновляем за 60 секунд до истечения, чтоб не словить 401 на границе.
        if self._access_token and now < (self._access_expires_at - 60):
            return self._access_token

        rq_uid = str(uuid.uuid4())
        headers = {
            "Authorization": f"Basic {self._auth_key}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": rq_uid,
        }
        data = {"scope": self._scope}

        try:
            async with httpx.AsyncClient(verify=self._verify_ssl,
                                          timeout=self._oauth_timeout) as cli:
                resp = await cli.post(GIGACHAT_OAUTH_URL, headers=headers, data=data)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500] if e.response else "(no body)"
            raise LLMClientError(
                f"GigaChat OAuth failed: HTTP {e.response.status_code} — {body}"
            ) from e
        except Exception as e:
            raise LLMClientError(f"GigaChat OAuth network error: {e}") from e

        self._access_token = payload.get("access_token")
        # expires_at приходит в МИЛЛИСЕКУНДАХ UNIX-времени.
        expires_at_ms = payload.get("expires_at")
        if expires_at_ms:
            self._access_expires_at = float(expires_at_ms) / 1000.0
        else:
            # Дефолт — 25 минут, чтобы перевыпустить заранее.
            self._access_expires_at = now + 1500

        if not self._access_token:
            raise LLMClientError(
                f"GigaChat OAuth: no access_token in response: {payload}"
            )
        return self._access_token

    async def chat(
        self,
        messages: list[dict],
        *,
        model: str = "GigaChat",
        temperature: float = 0.3,
        max_tokens: int = 1500,
        timeout: int = 60,
    ) -> LLMResponse:
        """Один синхронный chat-запрос. Не стримит."""
        access_token = await self._ensure_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(verify=self._verify_ssl,
                                          timeout=timeout) as cli:
                resp = await cli.post(GIGACHAT_CHAT_URL, headers=headers, json=body)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPStatusError as e:
            body_text = e.response.text[:500] if e.response else "(no body)"
            raise LLMClientError(
                f"GigaChat chat failed: HTTP {e.response.status_code} — {body_text}"
            ) from e
        except Exception as e:
            raise LLMClientError(f"GigaChat chat network error: {e}") from e

        choices = payload.get("choices") or []
        if not choices:
            raise LLMClientError(f"GigaChat: empty choices in response: {payload}")
        text = (choices[0].get("message") or {}).get("content") or ""

        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            prompt_tokens=usage.get("prompt_tokens"),
            response_tokens=usage.get("completion_tokens"),
            raw=payload,
        )


def make_client(
    provider: str,
    token: str,
    *,
    base_url: Optional[str] = None,
) -> LLMClient:
    """Фабрика LLM-клиента по строковому имени провайдера.

    Сейчас поддерживается gigachat_*; в будущем — local_vllm/ollama
    через openai-compat базу с base_url.
    """
    if provider.startswith("gigachat"):
        return GigaChatClient(token_authorization_key=token, verify_ssl=False)
    if provider in ("local_vllm", "openai_compat"):
        # TODO L8+: реализовать OpenAICompatClient(base_url=base_url, api_key=token).
        raise LLMClientError(
            f"Provider {provider!r} not implemented yet — only gigachat_* available."
        )
    raise LLMClientError(f"Unknown LLM provider: {provider!r}")


__all__ = [
    "LLMClient", "GigaChatClient", "LLMResponse", "LLMClientError",
    "make_client",
]
