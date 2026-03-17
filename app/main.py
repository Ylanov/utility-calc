import logging
import sentry_sdk
import redis.asyncio as redis
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi_limiter import FastAPILimiter
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend

from sqlalchemy.future import select
from passlib.context import CryptContext

from app.core.config import settings
from app.core.database import ArsenalSessionLocal, GsmSessionLocal
from app.modules.arsenal.models import ArsenalUser
from app.modules.gsm.models import GsmUser
from app.modules.utility.routers import settings as settings_router
from prometheus_fastapi_instrumentator import Instrumentator

# --- ЖКХ-1 ---
from app.modules.utility.routers import (
    admin_periods,
    client_readings,
    admin_reports,
    auth_routes,
    tariffs,
    admin_readings,
    users,
    admin_adjustments,
    admin_user_ops,
    financier,
)

from app.modules.telegram import telegram_app

# --- АРСЕНАЛ ---
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
    auth as arsenal_auth
)

# --- ГСМ ---
from app.modules.gsm import (
    routes as gsm_routes,
    auth as gsm_auth,
    reports as gsm_reports
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

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

    logger.info("Starting application initialization")

    # Redis
    try:
        redis_client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True
        )

        await FastAPILimiter.init(redis_client)

        FastAPICache.init(
            RedisBackend(redis_client),
            prefix="fastapi-cache"
        )

        logger.info("Redis connected successfully")

    except Exception as error:
        logger.error(f"Redis connection failed: {error}")

    # Admin checks
    try:
        await ensure_admin_exists(ArsenalSessionLocal, ArsenalUser)
        logger.info("Arsenal admin ensured")
    except Exception as e:
        logger.error(f"Failed to ensure Arsenal admin: {e}")

    try:
        await ensure_admin_exists(GsmSessionLocal, GsmUser)
        logger.info("GSM admin ensured")
    except Exception as e:
        logger.error(f"Failed to ensure GSM admin: {e}")

    yield

    logger.info("Application shutdown")


# =====================================================================
# FASTAPI
# =====================================================================

app = FastAPI(
    title="Utility Calculator & Arsenal & GSM",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None
)
Instrumentator().instrument(app).expose(app, endpoint="/metrics")
# =====================================================================
# MIDDLEWARES
# =====================================================================

# 1. Trusted Hosts (разрешаем проксирование через NPM и Tailscale)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["asy-tk.ru", "www.asy-tk.ru", "localhost", "127.0.0.1", "*"]
)

# 2. CORS
allowed_origins: List[str] = getattr(settings, "ALLOWED_ORIGINS", [])

if not allowed_origins:
    logger.warning("ALLOWED_ORIGINS not set. Using localhost")
    allowed_origins = [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:8000"
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Authorization",
        "X-Requested-With",
        "Accept"
    ],
)

# 3. Security Headers
@app.middleware("http")
async def add_security_headers(request: Request, call_next):

    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"

    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response


# =====================================================================
# SAFE ADMIN CREATION
# =====================================================================

async def ensure_admin_exists(session_factory, user_model):

    async with session_factory() as db:

        result = await db.execute(
            select(user_model).where(user_model.username == "admin")
        )

        admin = result.scalars().first()

        if not admin:

            logger.warning("Admin user not found. Creating default admin")

            admin = user_model(
                username="admin",
                hashed_password=pwd_context.hash("admin"),
                role="admin",
                object_id=None
            )

            db.add(admin)

            await db.commit()

        else:
            logger.info("Admin already exists")


# =====================================================================
# ROUTERS
# =====================================================================

# --- Health Check ---
@app.get("/health", include_in_schema=False)
def health_check():
    """Эндпоинт для проверки жизнеспособности контейнера Docker и Nginx"""
    return {"status": "ok"}


# --- ЖКХ ---
app.include_router(auth_routes.router)
app.include_router(users.router)
app.include_router(tariffs.router)
app.include_router(client_readings.router)
app.include_router(admin_readings.router)
app.include_router(admin_periods.router)
app.include_router(admin_reports.router)
app.include_router(admin_user_ops.router)
app.include_router(admin_adjustments.router)
app.include_router(financier.router)
app.include_router(telegram_app.router)
app.include_router(settings_router.router)

# --- АРСЕНАЛ ---
app.include_router(arsenal_auth.router)
app.include_router(arsenal_system.router, prefix="/api/arsenal")
app.include_router(arsenal_objects.router, prefix="/api/arsenal")
app.include_router(arsenal_nomenclature.router, prefix="/api/arsenal")
app.include_router(arsenal_documents.router, prefix="/api/arsenal")
app.include_router(arsenal_users_router.router, prefix="/api/arsenal")

app.include_router(arsenal_reports.router, prefix="/api/arsenal")

app.include_router(arsenal_routes.router)

# --- ГСМ ---
app.include_router(gsm_routes.router)
app.include_router(gsm_auth.router)
app.include_router(gsm_reports.router)


# =====================================================================
# FRONTEND
# =====================================================================

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/static/portal.html")

app.mount(
    "/static",
    StaticFiles(directory="static", html=True),
    name="static"
)