# app/core/request_context.py
"""
Контекст текущего HTTP-запроса.

Хранит request_id (UUID) и user_id в contextvars — доступны из любой функции,
без проброса через аргументы. Логирование, audit_log и Celery-задачи
могут читать их напрямую.

Использование:
    from app.core.request_context import current_request_id, current_user_id

    logger.info("...")  # request_id+user_id автоматически попадут в JSON-лог
    request_id = current_request_id.get()  # явное получение
"""
from __future__ import annotations

import contextvars
import json
import logging
from datetime import datetime, timezone
from typing import Optional

# Default "-" чтобы в логах фоновых задач (без HTTP) не было пустоты.
current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
# user_id — заполняется в get_current_user (auth dependency), помогает
# фильтровать логи в Sentry/Loki по конкретному жильцу.
current_user_id: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "user_id", default=None
)


class RequestIdFilter(logging.Filter):
    """
    Подсовывает request_id и user_id в каждый LogRecord. Полезен и для
    текстового форматтера (через `%(request_id)s`), и для JsonFormatter ниже.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id.get()
        record.user_id = current_user_id.get()
        return True


# Стандартные атрибуты LogRecord, которые НЕ нужно дублировать в JSON-вывод
# (они либо уже сериализованы как top-level поля, либо служебные).
_RESERVED_LOG_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs",
    "message", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    "request_id", "user_id",
}


class JsonFormatter(logging.Formatter):
    """
    Минимальный JSON-форматтер без внешних зависимостей.

    Каждая строка лога — самостоятельный JSON-объект с фиксированной схемой:
        {ts, level, logger, msg, request_id, user_id, [exc, file, line, ...extras]}

    Удобно для grep'а через `jq`, для агрегации в Loki/CloudWatch/Sentry, и
    для парсинга шеллом без regex-боли. Если в logger.info(..., extra={"x": 1})
    передан extra — поля попадают в корень JSON.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        user_id = getattr(record, "user_id", None)
        if user_id is not None:
            payload["user_id"] = user_id

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = record.stack_info

        # extras (logger.info(..., extra={"foo": "bar"})) — добавляем в корень,
        # чтобы Loki/CloudWatch их подсветили как отдельные поля.
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
