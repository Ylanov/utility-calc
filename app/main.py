from fastapi import FastAPI
from fastapi import Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import ORJSONResponse
import redis.asyncio as redis
from fastapi_limiter import FastAPILimiter
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
import logging
import sentry_sdk
from sqlalchemy.future import select
from passlib.context import CryptContext

from app.core.config import settings
from app.core.database import ArsenalSessionLocal, GsmSessionLocal

# Импортируем модели пользователей
from app.modules.arsenal.models import ArsenalUser
from app.modules.gsm.models import GsmUser

# Импортируем роутеры
from app.modules.utility.routers import admin_periods, client_readings, admin_reports, auth_routes, \
    tariffs, admin_readings, users, admin_adjustments, admin_user_ops, financier
from app.modules.telegram import telegram_app
from app.modules.arsenal import reports as arsenal_reports, routes as arsenal_routes, auth as arsenal_auth
from app.modules.gsm import routes as gsm_routes, auth as gsm_auth, reports as gsm_reports

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Настройка хеширования
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
    )

app = FastAPI(
    title="Utility Calculator & Arsenal & GSM",
    version="2.0.0",
    default_response_class=ORJSONResponse
)

# Подключение роутеров
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

app.include_router(arsenal_routes.router)
app.include_router(arsenal_auth.router)
app.include_router(arsenal_reports.router)

app.include_router(gsm_routes.router)
app.include_router(gsm_auth.router)
app.include_router(gsm_reports.router)

app.mount("/static", StaticFiles(directory="static", html=False), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# =====================================================================
# ФУНКЦИЯ СОЗДАНИЯ И ОБНОВЛЕНИЯ АДМИНОВ
# =====================================================================
async def create_default_admins():
    """
    Создает пользователя 'admin', если его нет.
    Если он есть, но роль не 'admin' — обновляет роль.
    """
    default_password = "admin"
    hashed_pw = pwd_context.hash(default_password)

    # 1. Проверка Арсенала
    try:
        async with ArsenalSessionLocal() as db:
            result = await db.execute(select(ArsenalUser).where(ArsenalUser.username == "admin"))
            user = result.scalars().first()

            if not user:
                logger.info("🛠 Creating default admin for ARSENAL...")
                admin = ArsenalUser(username="admin", hashed_password=hashed_pw, role="admin")
                db.add(admin)
                await db.commit()
            elif user.role != "admin":
                # 🔥 ИСПРАВЛЕНИЕ: Если юзер есть, но роль не та - обновляем
                logger.info("🛠 Fixing admin role for ARSENAL...")
                user.role = "admin"
                db.add(user)
                await db.commit()

    except Exception as e:
        logger.error(f"Failed to check/create Arsenal admin: {e}")

    # 2. Проверка ГСМ
    try:
        async with GsmSessionLocal() as db:
            result = await db.execute(select(GsmUser).where(GsmUser.username == "admin"))
            user = result.scalars().first()

            if not user:
                logger.info("🛢 Creating default admin for GSM...")
                admin = GsmUser(username="admin", hashed_password=hashed_pw, role="admin")
                db.add(admin)
                await db.commit()
            elif user.role != "admin":
                # 🔥 ИСПРАВЛЕНИЕ: Если юзер есть, но роль не та - обновляем
                logger.info("🛢 Fixing admin role for GSM...")
                user.role = "admin"
                db.add(user)
                await db.commit()

    except Exception as e:
        logger.error(f"Failed to check/create GSM admin: {e}")


@app.on_event("startup")
async def startup_event():
    logger.info("Starting application worker...")
    try:
        redis_client = redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
        await FastAPILimiter.init(redis_client)
        FastAPICache.init(RedisBackend(redis_client), prefix="fastapi-cache")
        logger.info("Redis connected.")
    except Exception as error:
        logger.warning(f"Redis unavailable: {error}")

    await create_default_admins()
    logger.info("Application worker startup complete.")