# app/core/middleware/request_id.py
"""
Middleware request_id — присваивает каждому HTTP-запросу уникальный UUID,
чтобы все логи внутри обработки запроса можно было связать одной строкой.

Поведение:
- Если клиент прислал заголовок X-Request-ID — используем его.
  (Полезно для трассировки между фронтом, мобильным и бэком.)
- Если нет — генерируем новый UUID v4.
- Возвращаем тот же ID в ответе (заголовок X-Request-ID),
  чтобы фронт мог показать его пользователю при ошибке («сообщите этот код в поддержку»).
- ID попадает в contextvar → автоматически в каждый log.record через RequestIdFilter.
"""
from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.request_context import current_request_id


HEADER_NAME = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Берём ID из заголовка если он валиден; иначе генерим.
        incoming = request.headers.get(HEADER_NAME, "").strip()
        request_id = incoming if _is_safe_id(incoming) else uuid.uuid4().hex

        # contextvars — потокобезопасны, привязаны к async-таску.
        # При завершении dispatch значение «откатывается» через token.reset().
        token = current_request_id.set(request_id)
        try:
            response = await call_next(request)
            response.headers[HEADER_NAME] = request_id
            return response
        finally:
            current_request_id.reset(token)


def _is_safe_id(value: str) -> bool:
    """Защита от инъекций в логи: только hex/uuid-подобный формат, до 64 символов."""
    if not value or len(value) > 64:
        return False
    return all(c.isalnum() or c == "-" for c in value)
