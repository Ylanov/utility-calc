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
from sqlalchemy.future import select  # Для проверки существования админа
from passlib.context import CryptContext  # Для хеширования пароля

from app.config import settings
from app.database import ArsenalSessionLocal, GsmSessionLocal  # Импортируем сессии

# Импортируем модели пользователей
from app.arsenal.models import ArsenalUser
from app.gsm.models import GsmUser

# Импортируем роутеры
from app.routers import (
    auth_routes, users, tariffs, client_readings,
    admin_readings, admin_periods, admin_reports,
    admin_user_ops, admin_adjustments, financier, telegram_app
)
from app.arsenal import routes as arsenal_routes
from app.arsenal import auth as arsenal_auth
from app.arsenal import reports as arsenal_reports
from app.gsm import routes as gsm_routes
from app.gsm import auth as gsm_auth
from app.gsm import reports as gsm_reports

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Настройка хеширования для создания дефолтного админа
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

# Подключение роутеров ЖКХ
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

# Подключение роутеров Арсенала
app.include_router(arsenal_routes.router)
app.include_router(arsenal_auth.router)
app.include_router(arsenal_reports.router)

# Подключение роутеров ГСМ
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
# ФУНКЦИЯ СОЗДАНИЯ ДЕФОЛТНЫХ АДМИНОВ
# =====================================================================
async def create_default_admins():
    """
    Проверяет наличие пользователей 'admin' в базах Арсенала и ГСМ.
    Если их нет — создает с паролем 'admin'.
    """
    default_password = "admin"
    hashed_pw = pwd_context.hash(default_password)

    # 1. Проверка Арсенала
    try:
        async with ArsenalSessionLocal() as db:
            result = await db.execute(select(ArsenalUser).where(ArsenalUser.username == "admin"))
            if not result.scalars().first():
                logger.info("🛠 Creating default admin for ARSENAL...")
                admin = ArsenalUser(
                    username="admin",
                    hashed_password=hashed_pw,
                    role="admin"  # Сразу права админа
                )
                db.add(admin)
                await db.commit()
                logger.info("✅ Arsenal admin created (Login: admin / Pass: admin)")
    except Exception as e:
        logger.error(f"Failed to check/create Arsenal admin: {e}")

    # 2. Проверка ГСМ
    try:
        async with GsmSessionLocal() as db:
            result = await db.execute(select(GsmUser).where(GsmUser.username == "admin"))
            if not result.scalars().first():
                logger.info("🛢 Creating default admin for GSM...")
                admin = GsmUser(
                    username="admin",
                    hashed_password=hashed_pw,
                    role="admin"  # Сразу права админа
                )
                db.add(admin)
                await db.commit()
                logger.info("✅ GSM admin created (Login: admin / Pass: admin)")
    except Exception as e:
        logger.error(f"Failed to check/create GSM admin: {e}")


# =====================================================================
# СОБЫТИЯ СТАРТА ПРИЛОЖЕНИЯ
# =====================================================================
@app.on_event("startup")
async def startup_event():
    logger.info("Starting application worker...")

    # 1. Подключаем Redis
    try:
        redis_client = redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
        await FastAPILimiter.init(redis_client)
        FastAPICache.init(RedisBackend(redis_client), prefix="fastapi-cache")
        logger.info("Redis connected.")
    except Exception as error:
        logger.warning(f"Redis unavailable: {error}")

    # 2. Создаем дефолтных админов (если их нет)
    await create_default_admins()

    logger.info("Application worker startup complete.")