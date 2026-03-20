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
# API PREFIXES (ENTERPRISE STYLE)
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

    # Redis
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

    # Admin creation
    if APP_MODE in ("all", "arsenal_gsm"):
        await ensure_admin_exists_safe(ArsenalSessionLocal, ArsenalUser, "Arsenal")
        await ensure_admin_exists_safe(GsmSessionLocal, GsmUser, "GSM")

    yield

    logger.info("Application shutdown")

# =====================================================================
# FASTAPI INIT
# =====================================================================
app = FastAPI(
    title="Utility Calculator & Arsenal & GSM",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# =====================================================================
# MIDDLEWARES
# =====================================================================
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["asy-tk.ru", "www.asy-tk.ru", "localhost", "127.0.0.1", "*"],
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

    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response

# =====================================================================
# HELPERS
# =====================================================================
async def ensure_admin_exists_safe(session_factory, user_model, name: str):
    try:
        async with session_factory() as db:
            result = await db.execute(
                select(user_model).where(user_model.username == "admin")
            )
            admin = result.scalars().first()

            if not admin:
                logger.warning(f"{name}: admin not found, creating")
                admin = user_model(
                    username="admin",
                    hashed_password=pwd_context.hash("admin"),
                    role="admin",
                    object_id=None,
                )
                db.add(admin)
                await db.commit()
            else:
                logger.info(f"{name}: admin exists")

    except Exception as e:
        logger.error(f"{name}: admin init failed: {e}")

# =====================================================================
# ROUTERS REGISTRATION (ENTERPRISE)
# =====================================================================
def register_jkh_routes(app: FastAPI):
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


def register_arsenal_routes(app: FastAPI):
    router = APIRouter(prefix=ARSENAL_API_PREFIX)

    router.include_router(arsenal_system.router)
    router.include_router(arsenal_objects.router)
    router.include_router(arsenal_nomenclature.router)
    router.include_router(arsenal_documents.router)
    router.include_router(arsenal_users_router.router)
    router.include_router(arsenal_reports.router)

    app.include_router(arsenal_auth.router)
    app.include_router(router)
    app.include_router(arsenal_routes.router)


def register_gsm_routes(app: FastAPI):
    router = APIRouter(prefix=GSM_API_PREFIX)

    router.include_router(gsm_routes.router)
    router.include_router(gsm_auth.router)
    router.include_router(gsm_reports.router)

    app.include_router(router)

# =====================================================================
# HEALTH
# =====================================================================
@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "mode": APP_MODE}

# =====================================================================
# ROUTER LOADING
# =====================================================================
if APP_MODE in ("all", "jkh"):
    register_jkh_routes(app)

if APP_MODE in ("all", "arsenal_gsm"):
    register_arsenal_routes(app)
    register_gsm_routes(app)

# =====================================================================
# FRONTEND
# =====================================================================
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/static/portal.html")

app.mount(
    "/static",
    StaticFiles(directory="static", html=True),
    name="static",
)

@app.middleware("http")
async def catch_exceptions(request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        import traceback
        print("\n🔥 BACKEND ERROR 🔥")
        traceback.print_exc()
        print("🔥 END ERROR 🔥\n")

        return JSONResponse(
            status_code=500,
            content={"detail": str(e)}
        )