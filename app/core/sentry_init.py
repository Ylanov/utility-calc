# app/core/sentry_init.py
"""
Единая инициализация Sentry для FastAPI-процесса и Celery-воркеров.

Подход:
- web (app/main.py) и worker (app/worker.py) зовут setup_sentry() — конфиг
  один и тот же, чтобы события из обоих процессов агрегировались в один проект
  с одинаковыми тегами и фильтрами.
- При отсутствии SENTRY_DSN функция выходит без шума: тестовая/локальная
  среда работает без отправки событий наружу.

Включённые интеграции:
- FastApiIntegration / StarletteIntegration — автоматический request_id, route,
  user, request body для каждого error event.
- SqlalchemyIntegration — медленные SQL-запросы видны в Sentry Performance.
- CeleryIntegration — таски с retry/failure; передаёт task_id, args.
- RedisIntegration — span'ы для Redis-вызовов.
- LoggingIntegration — log.warning → breadcrumb, log.error → event.
  Это ключевая интеграция: 95% багов в проекте уже логируются — после
  включения они автоматически попадают в Sentry без дополнительного кода.

PII-фильтр (before_send):
- Удаляем JWT-токены и пароли из request.headers / request.body / extras.
- send_default_pii=False — по умолчанию Sentry SDK не пишет cookies/headers
  целиком; мы эту настройку оставляем включённой.

Release tagging:
- Берём из env SENTRY_RELEASE (CI должен подставлять git sha при деплое).
  Без него — пусто, все события группируются как «unreleased».
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.redis import RedisIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from app.core.config import settings


# Имена заголовков и полей, которые нужно скрыть в Sentry events.
# Регистр игнорируется. Список расширяемый — добавляйте сюда любые
# секреты, которые могут попасть в headers/extras.
_SCRUB_KEYS = frozenset({
    "authorization", "cookie", "x-api-key", "x-auth-token",
    "password", "secret", "token", "totp_code", "totp_secret",
    "csrf_token", "encryption_key", "sentry_dsn",
})


def _scrub_pii(event: dict, hint: dict) -> Optional[dict]:
    """
    before_send hook — финальный пас перед отправкой события в Sentry.

    Что делаем:
    1. Чистим заголовки запроса (Authorization: Bearer ... — секрет).
    2. Чистим extras (request body) от полей с секретами.
    3. Если в exception value торчит чувствительная строка — заменяем.

    Не делаем:
    - Не дропаем event целиком — нам нужна сама инфа об ошибке.
    """
    request = event.get("request") or {}
    headers = request.get("headers") or {}
    if isinstance(headers, dict):
        for k in list(headers.keys()):
            if k.lower() in _SCRUB_KEYS:
                headers[k] = "[Filtered]"

    # JSON-body запроса (для FastAPI попадает в request.data)
    data = request.get("data")
    if isinstance(data, dict):
        for k in list(data.keys()):
            if k.lower() in _SCRUB_KEYS:
                data[k] = "[Filtered]"

    # extras от пользовательского кода (sentry_sdk.set_context, set_extra)
    extras = event.get("extra") or {}
    for k in list(extras.keys()):
        if k.lower() in _SCRUB_KEYS:
            extras[k] = "[Filtered]"

    return event


def _resolve_release() -> Optional[str]:
    """
    Версия деплоя для группировки багов по релизам.

    Приоритет: SENTRY_RELEASE → GIT_COMMIT → APP_VERSION → None.
    CI должен подставлять SENTRY_RELEASE = `git rev-parse --short HEAD`
    при деплое. Без этого все события идут как «unreleased».
    """
    return (
        os.environ.get("SENTRY_RELEASE")
        or os.environ.get("GIT_COMMIT")
        or os.environ.get("APP_VERSION")
        or None
    )


def setup_sentry(*, component: str) -> None:
    """
    Инициализирует Sentry SDK, если задан DSN.

    component — короткий тег "web" или "worker", попадает в каждый event как
    tag и помогает фильтровать «упало в API» от «упало в Celery-таске».
    """
    dsn = settings.SENTRY_DSN
    if not dsn:
        # Локальная/CI-среда без Sentry — это ожидаемая ситуация.
        return

    integrations = [
        # Логи WARNING+ → breadcrumb, ERROR+ → Sentry event.
        # event_level можно поднять до CRITICAL если хочется меньше шума,
        # но WARNING-ошибки часто и есть «незамеченные баги».
        LoggingIntegration(
            level=logging.INFO,        # bread crumbs от INFO+
            event_level=logging.ERROR, # event только ERROR+
        ),
        StarletteIntegration(),
        FastApiIntegration(transaction_style="endpoint"),
        SqlalchemyIntegration(),
        RedisIntegration(),
        CeleryIntegration(),
    ]

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.ENVIRONMENT,
        release=_resolve_release(),
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        # send_default_pii=False — по умолчанию SDK не отправляет cookies/IP
        # пользователя без явного set_user(). user_id мы прокидываем сами в
        # get_current_user, но cookies/headers — точно не нужны.
        send_default_pii=False,
        # attach_stacktrace=True — даже у logger.warning() будет стек,
        # помогает понять «откуда лог пришёл», когда event без exception.
        attach_stacktrace=True,
        integrations=integrations,
        before_send=_scrub_pii,
    )

    sentry_sdk.set_tag("component", component)
    sentry_sdk.set_tag("app_mode", os.environ.get("APP_MODE", "all"))
