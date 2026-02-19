from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import ORJSONResponse  # <-- Быстрый JSON
import redis.asyncio as redis
from fastapi_limiter import FastAPILimiter
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
import logging
import sentry_sdk
from app.config import settings
from app.routers import (
    auth_routes, users, tariffs, client_readings, admin_readings,
    admin_periods, admin_reports, admin_user_ops, admin_adjustments, financier
)
from app.arsenal import routes as arsenal_routes
from app.arsenal import auth as arsenal_auth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
    )

app = FastAPI(
    title="Utility Calculator",
    version="1.0.0",
    default_response_class=ORJSONResponse  # <-- Ускоряет сериализацию ответов в разы
)

# ... подключение роутеров ...
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
app.include_router(arsenal_auth.router)

app.mount("/static", StaticFiles(directory="static", html=False), name="static")


@app.on_event("startup")
async def startup_event():
    logger.info("Starting application worker...")
    try:
        redis_client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True
        )

        # Инициализация Rate Limiter
        await FastAPILimiter.init(redis_client)

        # Инициализация Cache (Префикс ключей чтобы не пересекаться с Celery)
        FastAPICache.init(RedisBackend(redis_client), prefix="fastapi-cache")

        logger.info("Redis connected (Rate Limiter + Cache).")
    except Exception as error:
        logger.warning(f"Redis unavailable: {error}")
    logger.info("Application worker startup complete.")