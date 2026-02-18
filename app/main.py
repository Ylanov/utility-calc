# app/main.py
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import redis.asyncio as redis
from fastapi_limiter import FastAPILimiter
import logging

from app.config import settings
from app.routers import (
    auth_routes,
    users,
    tariffs,
    client_readings,
    admin_readings,
    admin_periods,
    admin_reports,
    admin_user_ops,
    admin_adjustments,
    financier
)
from app.arsenal import routes as arsenal_routes

# --- ВАЖНО: Импортируем движки и метаданные моделей ---
from app.database import engine, Base  # Для ЖКХ
from app.database import arsenal_engine, ArsenalBase  # Для Арсенала

# -------------------------------------------------
# LOGGING
# -------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------------------------
# APP
# -------------------------------------------------
app = FastAPI(
    title="Utility Calculator",
    version="1.0.0"
)

# -------------------------------------------------
# ROUTERS
# -------------------------------------------------
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

app.include_router(arsenal_routes.router)

# -------------------------------------------------
# STATIC
# -------------------------------------------------
app.mount(
    "/static",
    StaticFiles(directory="static", html=False),
    name="static"
)


# -------------------------------------------------
# STARTUP
# -------------------------------------------------
@app.on_event("startup")
async def startup_event():
    """
    Выполняется при старте приложения:
    1. Создает таблицы в обеих базах данных.
    2. Инициализирует Redis.
    """

    # --- ШАГ 1: Создание таблиц (если их нет) ---
    logger.info("Initializing database tables...")

    # 1.1 Создаем таблицы для ЖКХ (utility_db)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Utility DB tables checked/created.")

    # 1.2 Создаем таблицы для Арсенала (arsenal_db)
    async with arsenal_engine.begin() as conn:
        await conn.run_sync(ArsenalBase.metadata.create_all)
        logger.info("Arsenal DB tables checked/created.")

    # --- ШАГ 2: Инициализация Redis ---
    try:
        redis_client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True
        )
        await FastAPILimiter.init(redis_client)
        logger.info("Redis connected for rate limiting.")
    except Exception as error:
        logger.warning(f"Redis unavailable, rate limiting is disabled: {error}")

    logger.info("Application worker startup complete.")