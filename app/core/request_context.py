# app/core/request_context.py
"""
Контекст текущего HTTP-запроса.

Хранит request_id (UUID) в contextvars — доступен из любой функции,
без проброса через аргументы. Логирование, audit_log и Celery-задачи
могут читать его напрямую.

Использование:
    from app.core.request_context import current_request_id

    logger.info("...")  # request_id попадёт в формат через RequestIdFilter
    request_id = current_request_id.get()  # явное получение
"""
from __future__ import annotations

import contextvars
import logging

# Default "-" чтобы в логах фоновых задач (без HTTP) не было пустоты.
current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


class RequestIdFilter(logging.Filter):
    """
    Подсовывает request_id в каждый LogRecord. Используется в logging.Formatter
    через `%(request_id)s`. Без фильтра форматтер падал бы с KeyError.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id.get()
        return True
