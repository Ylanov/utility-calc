# app/core/middleware/error_capture.py
"""
ErrorCaptureMiddleware — копилка backend-ошибок (E3-A, 28.05.2026).

Цель: каждое unhandled exception (которое в итоге станет 500) сохраняется
в таблицу error_log с traceback + URL + user + request_id + авто-собранным
контекстом. Админ видит ошибку в /api/admin/errors и копирует в чат с AI.

Принципы:
1. Middleware ловит ВСЁ что падает в чейне call_next — это unhandled
   exceptions сервиса. После сохранения exception пробрасывается дальше
   (FastAPI ставит 500 + Sentry).
2. Сохранение ошибки идёт в ОТДЕЛЬНОЙ AsyncSession (request-scoped session
   уже broken после исключения), причём в try/except — если сохранение
   само упало, request не должен «удвоить ошибку».
3. Для 4xx (HTTPException) middleware НЕ работает — их FastAPI обрабатывает
   до того как exception всплывёт сюда. Для 4xx используем отдельный
   exception_handler в main.py (см. main.py).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

logger = logging.getLogger(__name__)


# Пути, для которых НЕ логируем 500 в копилку. Health-checks и /metrics
# падают редко, но если что — Sentry уже их видит, не засоряем error_log.
_SKIP_PATHS = ("/health", "/healthz", "/metrics", "/favicon.ico")


class ErrorCaptureMiddleware(BaseHTTPMiddleware):
    """Перехватывает unhandled exceptions и сохраняет в error_log."""

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            # Сохраняем — но не ломаем поток, если log_error сам упал.
            try:
                if not _should_skip(request.url.path):
                    await _save_to_error_log(request, exc)
            except Exception as save_err:
                logger.warning(
                    "[error_capture] failed to save error_log: %s", save_err,
                )
            # Пробрасываем дальше — FastAPI вернёт 500, Sentry поймает.
            raise


def _should_skip(path: str) -> bool:
    return any(path.startswith(p) for p in _SKIP_PATHS)


async def _save_to_error_log(request: Request, exc: BaseException) -> None:
    """Сохраняет запись об ошибке. Идёт в отдельной AsyncSession."""
    # Лениво импортируем чтобы избежать круговых импортов на старте.
    from app.core.database import AsyncSessionLocal
    from app.core.error_logger import log_error

    body = await _read_safe_body(request)
    user_id, user_username = _extract_user(request)
    request_id = request.headers.get("X-Request-ID")

    async with AsyncSessionLocal() as db:
        await log_error(
            db,
            source="backend",
            level="error",
            http_method=request.method,
            http_path=request.url.path,
            http_status=500,
            exc=exc,
            request_body=body,
            user_id=user_id,
            user_username=user_username,
            request_id=request_id,
        )


async def _read_safe_body(request: Request) -> Any:
    """Безопасно читает body для лога. Не больше 10KB, секретные ключи маскирует."""
    # Тело аутентификационных ручек НЕ логируем вовсе: там пароли/коды/токены,
    # причём /api/token — form-urlencoded (раньше не парсился как JSON и пароль
    # уходил в error_log в открытом виде, видимый админу).
    try:
        _path = request.url.path or ""
        if _path.startswith("/api/token") or _path.startswith("/api/auth"):
            return "***тело auth-запроса не логируется***"
    except Exception:
        pass
    try:
        raw = await request.body()
        if not raw:
            return None
        if len(raw) > 10_240:
            return {
                "_truncated": True,
                "_size_bytes": len(raw),
                "_first_500": raw[:500].decode("utf-8", errors="replace"),
            }
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            # Не JSON (например form-urlencoded) — маскируем секретные form-поля,
            # а не возвращаем сырую строку (там мог быть пароль).
            text = raw.decode("utf-8", errors="replace")[:2000]
            try:
                from urllib.parse import parse_qsl, urlencode
                pairs = parse_qsl(text, keep_blank_values=True)
                if pairs:
                    return urlencode([
                        (k, "***" if _is_secret_key(k) else v) for k, v in pairs
                    ])
            except Exception:
                pass
            return text[:1000]
        return _mask_secrets(parsed)
    except Exception:
        return None


# Подстроки имён полей, значения которых маскируем в логах ошибок (этот лог
# виден админам через /api/admin/errors и копируется в чат с ИИ). Подстрочное
# сравнение ловит new_password/old_password/refresh_token/csrf_token и т.п.
# Над-маскирование безобидных полей (zip_code) безопасно — это только лог.
_SECRET_KEYS = {"password", "hashed_password", "token", "secret", "api_key",
                "totp_code", "totp_secret", "otp", "encryption_key", "fernet"}


def _is_secret_key(name: str) -> bool:
    n = (name or "").lower()
    return any(s in n for s in _SECRET_KEYS)


def _mask_secrets(obj: Any) -> Any:
    """Рекурсивно заменяет значения секретных ключей на ***."""
    if isinstance(obj, dict):
        return {
            k: ("***" if _is_secret_key(k) else _mask_secrets(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_mask_secrets(v) for v in obj]
    return obj


def _extract_user(request: Request) -> tuple:
    """Достаёт id/username из request.state.user если есть.

    FastAPI Depends(get_current_user) кладёт User-объект в request.state
    через Sentry-интеграцию или ручной хук. В этом проекте get_current_user
    не пишет в state автоматически — поэтому fallback на None, и в логе
    user будет проставлен только если auth-зависимость явно его выставила.
    """
    user_id = None
    user_username = None
    state = getattr(request, "state", None)
    if state is not None:
        u = getattr(state, "user", None)
        if u is not None:
            user_id = getattr(u, "id", None)
            user_username = getattr(u, "username", None)
    return user_id, user_username
