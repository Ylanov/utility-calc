# app/main.py

import os
import logging
from contextlib import asynccontextmanager
from typing import List

import sentry_sdk
import redis.asyncio as redis

from fastapi import FastAPI, Request, APIRouter
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from fastapi_limiter import FastAPILimiter
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend

from sqlalchemy.future import select
from passlib.context import CryptContext

from prometheus_fastapi_instrumentator import Instrumentator
from fastapi.responses import JSONResponse

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
# API PREFIXES
# =====================================================================
API_PREFIX = "/api"
ARSENAL_API_PREFIX = f"{API_PREFIX}/arsenal"
GSM_API_PREFIX = f"{API_PREFIX}/gsm"

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

        FastAPICache.init(
            RedisBackend(redis_client),
            prefix="fastapi-cache",
        )

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
# ИСПРАВЛЕНИЕ: docs_url отключается в production — Swagger не должен
# быть доступен всем в боевой среде.
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
# MIDDLEWARES
# =====================================================================

# ИСПРАВЛЕНИЕ: убран wildcard "*" — он полностью нейтрализует TrustedHostMiddleware.
# Оставляем только реальные хосты проекта.
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

    # ИСПРАВЛЕНИЕ: добавлен Content-Security-Policy.
    # Ограничивает загрузку ресурсов только с доверенных источников,
    # что существенно снижает риск XSS-атак.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com cdnjs.cloudflare.com; "
        "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com data:; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )

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
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================

async def ensure_admin_exists_safe(session_local, model, label: str):
    """Создаёт администратора если его нет. Не падает при ошибке."""
    try:
        async with session_local() as db:
            result = await db.execute(select(model).where(model.role == "admin"))
            admin = result.scalars().first()
            if not admin:
                admin = model(
                    username="admin",
                    hashed_password=pwd_context.hash("admin"),
                    role="admin"
                )
                db.add(admin)
                await db.commit()
                logger.info(f"{label}: admin user created")
            else:
                logger.info(f"{label}: admin user already exists")
    except Exception as e:
        logger.error(f"{label}: Failed to ensure admin exists: {e}")


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
# =====================================================================
app.mount("/", StaticFiles(directory="static", html=True), name="static")