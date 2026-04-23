# app/main.py

import os
import logging
from contextlib import asynccontextmanager
from typing import List

import sentry_sdk
import redis.asyncio as redis

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from fastapi_limiter import FastAPILimiter
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend

from sqlalchemy.future import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from passlib.context import CryptContext

from prometheus_fastapi_instrumentator import Instrumentator

# === CORE ===
from app.core.config import settings
from app.core.database import ArsenalSessionLocal, GsmSessionLocal

# === MODELS ===
from app.modules.arsenal.models import ArsenalUser
from app.modules.gsm.models import GsmUser

# === ЖКХ ===
from app.modules.utility.routers import (
    admin_periods,
    client_readings,
    admin_reports,
    auth_routes,
    tariffs,
    admin_readings,
    users,
    rooms,
    admin_adjustments,
    admin_user_ops,
    financier,
    settings as settings_router,
    admin_dashboard,
    admin_initial_readings,
    admin_gsheets,
    admin_analyzer,
    admin_recalc,
    client_certificates,
    admin_certificates,
    app_releases,
    qr,
)

from app.modules.telegram import telegram_app
# === АРСЕНАЛ ===
from app.modules.arsenal.routers import (
    system as arsenal_system,
    objects as arsenal_objects,
    nomenclature as arsenal_nomenclature,
    documents as arsenal_documents,
    users as arsenal_users_router,
)

from app.modules.arsenal import (
    reports as arsenal_reports,
    routes as arsenal_routes,
    auth as arsenal_auth,
)

# === ГСМ ===
from app.modules.gsm import (
    routes as gsm_routes,
    auth as gsm_auth,
    reports as gsm_reports,
)

# =====================================================================
# LOGGING
#
# Структурированный формат с request_id из contextvars. RequestIdFilter
# подкладывает request_id в каждый LogRecord; форматтер показывает его
# в каждой строке. Это даёт сквозную трассировку HTTP-запроса по логам:
# можно `grep <request_id>` и увидеть всю цепочку обработки.
# =====================================================================
from app.core.request_context import RequestIdFilter
from app.core.middleware.request_id import RequestIdMiddleware

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] [req:%(request_id)s] %(message)s"
_root_handler = logging.StreamHandler()
_root_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_root_handler.addFilter(RequestIdFilter())

# Заменяем дефолтные хендлеры root-логгера на наш с фильтром.
logging.basicConfig(
    level=logging.INFO,
    handlers=[_root_handler],
    force=True,  # перебиваем basicConfig, который мог поставить uvicorn
)

# Прикрепляем фильтр к uvicorn-логгерам (access/error) — иначе их строки
# будут без request_id, и трассировка частично теряется.
for _name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
    _lg = logging.getLogger(_name)
    _lg.addFilter(RequestIdFilter())
    for _h in _lg.handlers:
        _h.addFilter(RequestIdFilter())

logger = logging.getLogger(__name__)

# =====================================================================
# SECURITY
# =====================================================================
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# =====================================================================
# APP MODE
# =====================================================================
APP_MODE = os.environ.get("APP_MODE", "all")

# =====================================================================
# SENTRY
# =====================================================================
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
    )


# =====================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================

async def ensure_admin_exists_safe(session_local, model, label: str):
    """
    Создаёт администратора если его нет. Не падает при ошибке.

    Раньше пароль был жёстко захардкожен как "admin" — что создавало
    предсказуемую привилегированную учётку на каждом свежем деплое
    Arsenal/GSM. Теперь пароль берётся из ENV ARSENAL_ADMIN_INITIAL_PASSWORD
    (или GSM_ADMIN_INITIAL_PASSWORD для GSM). Если ENV не задан — сидирование
    пропускается: админа создадут руками через db/утилиту миграции.

    Concurrency: INSERT ... ON CONFLICT DO NOTHING — атомарная операция,
    безопасная при параллельном старте нескольких воркеров Gunicorn.
    """
    # Label приходит "Arsenal" | "GSM" — берём env-переменную по шаблону.
    env_key = f"{label.upper()}_ADMIN_INITIAL_PASSWORD"
    initial_password = os.environ.get(env_key)

    if not initial_password:
        logger.warning(
            f"{label}: {env_key} не задан — пропускаем автосоздание admin. "
            "Создайте пользователя вручную или задайте ENV-переменную при деплое."
        )
        return

    if len(initial_password) < 12:
        # Защита от коротких паролей — чтобы нельзя было обойти жёсткий
        # контроль случайно заданным "admin1234".
        logger.error(
            f"{label}: {env_key} слишком короткий (< 12 символов). "
            "Автосоздание admin пропущено."
        )
        return

    try:
        async with session_local() as db:
            hashed_pw = pwd_context.hash(initial_password)

            stmt = (
                pg_insert(model)
                .values(
                    username="admin",
                    hashed_password=hashed_pw,
                    role="admin",
                )
                .on_conflict_do_nothing(index_elements=["username"])
            )

            await db.execute(stmt)
            await db.commit()
            logger.info(f"{label}: admin user ensured (created or already existed)")

    except Exception as e:
        logger.error(f"{label}: Failed to ensure admin exists: {e}", exc_info=True)


# =====================================================================
# LIFESPAN
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting application in mode: {APP_MODE.upper()}")

    try:
        redis_client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        await FastAPILimiter.init(redis_client)
        FastAPICache.init(RedisBackend(redis_client), prefix="fastapi-cache")
        logger.info("Redis connected")
    except Exception as error:
        logger.error(f"Redis connection failed: {error}")

    if APP_MODE in ("all", "arsenal_gsm"):
        await ensure_admin_exists_safe(ArsenalSessionLocal, ArsenalUser, "Arsenal")
        await ensure_admin_exists_safe(GsmSessionLocal, GsmUser, "GSM")

    yield

    logger.info("Application shutdown")


# =====================================================================
# FASTAPI INIT
# =====================================================================
IS_PRODUCTION = settings.ENVIRONMENT == "production"

app = FastAPI(
    title="Utility Calculator & Arsenal & GSM",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None,
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics")


# =====================================================================
# HEALTHCHECK ENDPOINT
#
# ИСПРАВЛЕНИЕ: эндпоинт /health отсутствовал — FastAPI возвращал 404,
# CI/CD pipeline и Docker healthcheck падали с кодом 000/404.
#
# ВАЖНО: регистрируется ДО подключения StaticFiles mount.
# StaticFiles монтируется на "/" и перехватывает ВСЕ запросы которые
# не совпали с роутами выше. Если /health зарегистрировать после mount —
# StaticFiles поймает его первым и вернёт 404.
# =====================================================================
@app.get("/health", tags=["System"], include_in_schema=False)
async def health_check():
    """
    Healthcheck для Docker, CI/CD и Nginx.
    Всегда возвращает 200 если сервис поднят.

    Раньше тело ответа раскрывало APP_MODE — это внутренний признак
    развёртывания (jkh/arsenal_gsm/all), публиковать его внешним
    сканерам и ботам не нужно.
    """
    return {"status": "ok"}


# =====================================================================
# MIDDLEWARES
# =====================================================================
# RequestIdMiddleware регистрируется ПЕРВЫМ (значит выполнится последним
# на пути запроса вверх и первым вниз), чтобы request_id был доступен
# во всём остальном middleware-стеке и хендлерах.
app.add_middleware(RequestIdMiddleware)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["asy-tk.ru", "www.asy-tk.ru", "localhost", "127.0.0.1"],
)

allowed_origins: List[str] = getattr(settings, "ALLOWED_ORIGINS", [])

if not allowed_origins:
    logger.warning("ALLOWED_ORIGINS not set, fallback to localhost")
    allowed_origins = [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    # ===============================================================
    # НАСТРОЙКА CSP (CONTENT SECURITY POLICY)
    # ===============================================================
    # Строгая политика для всей системы (по умолчанию)
    base_csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com cdnjs.cloudflare.com; "
        "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com data:; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )

    # Политика для модуля Арсенал (разрешаем CDN Tailwind Play)
    # connect-src нужен т.к. Tailwind Play CDN делает fetch-запросы в runtime
    arsenal_csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdnjs.cloudflare.com https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com cdnjs.cloudflare.com https://cdn.tailwindcss.com; "
        "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com data:; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' https://cdn.tailwindcss.com; "
        "frame-ancestors 'none';"
    )

    # Проверяем, обращается ли пользователь к файлам Арсенала
    if "arsenal" in request.url.path.lower():
        response.headers["Content-Security-Policy"] = arsenal_csp
    else:
        response.headers["Content-Security-Policy"] = base_csp

    return response


@app.middleware("http")
async def no_cache_api_headers(request: Request, call_next):
    response = await call_next(request)

    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    return response


# =====================================================================
# ROUTES — ЖКХ
# =====================================================================
app.include_router(auth_routes.router)
app.include_router(admin_periods.router)
app.include_router(client_readings.router)
app.include_router(admin_reports.router)
app.include_router(tariffs.router)
app.include_router(admin_readings.router)
app.include_router(users.router)
app.include_router(rooms.router)
app.include_router(admin_adjustments.router)
app.include_router(admin_user_ops.router)
app.include_router(financier.router)
app.include_router(settings_router.router)
app.include_router(telegram_app.router)
app.include_router(admin_dashboard.router)

app.include_router(admin_initial_readings.router)
app.include_router(admin_gsheets.router)
app.include_router(admin_analyzer.router)
app.include_router(admin_recalc.router)
app.include_router(client_certificates.router)
app.include_router(admin_certificates.router)
app.include_router(app_releases.router)
app.include_router(qr.router)

# =====================================================================
# ROUTES — АРСЕНАЛ
# =====================================================================
app.include_router(arsenal_auth.router)
app.include_router(arsenal_routes.router)
app.include_router(arsenal_reports.router)

# =====================================================================
# ROUTES — ГСМ
# =====================================================================
app.include_router(gsm_auth.router)
app.include_router(gsm_routes.router)
app.include_router(gsm_reports.router)

# =====================================================================
# CRAWLER / WELL-KNOWN 404 STUBS
#
# StaticFiles смонтирован ниже с html=True — это значит, что на любой
# несуществующий путь Starlette отдаёт index.html с кодом 200. Для SPA
# это правильно (deep-link роутер внутри JS берёт путь из location),
# но для ботов-сканеров, crawler'ов и well-known-файлов это создаёт
# путаницу: /robots.txt, /sitemap.xml, /.well-known/security.txt
# возвращают HTML-портал, и любой сканер думает что контент есть.
#
# Регистрируем явные 404 ДО mount'а StaticFiles, чтобы эти пути
# отбивались корректно, а SPA-deep-routing продолжал работать.
# =====================================================================
async def _serve_or_404(filename: str, media_type: str):
    """Отдаём реальный файл из static/ если он лежит на диске, иначе
    честный 404. Смысл: StaticFiles(html=True) на несуществующий путь
    возвращает index.html с 200 OK — crawler'ы видят HTML вместо
    robots.txt/sitemap.xml. Этот хелпер ломает такое поведение."""
    path = os.path.join("static", filename)
    if os.path.isfile(path):
        return FileResponse(path, media_type=media_type)
    raise HTTPException(status_code=404)


@app.get("/robots.txt", include_in_schema=False)
async def _robots_txt():
    return await _serve_or_404("robots.txt", "text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def _sitemap_xml():
    return await _serve_or_404("sitemap.xml", "application/xml")


@app.get("/.well-known/{path:path}", include_in_schema=False)
async def _no_well_known(path: str):
    # RFC 8615 пути. Пока никакой well-known инфраструктуры у нас нет
    # (ни ACME-challenge, ни security.txt) — возвращаем 404, а не SPA-HTML.
    # Когда понадобится — добавим конкретный хендлер для нужного пути.
    raise HTTPException(status_code=404)


# =====================================================================
# STATIC FILES
# Монтируется ПОСЛЕДНИМ — перехватывает все запросы которые не
# совпали с роутами FastAPI выше. /health должен быть зарегистрирован
# до этой строки, иначе StaticFiles вернёт 404.
# =====================================================================
app.mount("/", StaticFiles(directory="static", html=True), name="static")