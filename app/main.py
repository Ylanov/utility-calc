# app/main.py

import os
import logging
from contextlib import asynccontextmanager
from typing import List

import sentry_sdk
import redis.asyncio as redis

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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
# =====================================================================
logging.basicConfig(level=logging.INFO)
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

    ИСПРАВЛЕНИЕ: предыдущая версия делала SELECT → INSERT.
    При 4 воркерах Gunicorn все они одновременно стартуют, делают SELECT
    (все видят что нет admin), затем все пытаются INSERT →
    UniqueViolationError у 3 из 4 воркеров при каждом деплое.

    Решение: INSERT ... ON CONFLICT DO NOTHING — атомарная операция на уровне БД,
    безопасна при параллельном выполнении любого числа воркеров.
    """
    try:
        async with session_local() as db:
            hashed_pw = pwd_context.hash("admin")

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
    """
    return {"status": "ok", "mode": APP_MODE}


# =====================================================================
# MIDDLEWARES
# =====================================================================
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
# STATIC FILES
# Монтируется ПОСЛЕДНИМ — перехватывает все запросы которые не
# совпали с роутами FastAPI выше. /health должен быть зарегистрирован
# до этой строки, иначе StaticFiles вернёт 404.
# =====================================================================
app.mount("/", StaticFiles(directory="static", html=True), name="static")