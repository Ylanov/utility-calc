from fastapi import FastAPI
from fastapi import Request
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
from app.arsenal import reports as arsenal_reports

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
app.include_router(arsenal_reports.router)

app.mount("/static", StaticFiles(directory="static", html=False), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)

    # Запрещает браузеру "угадывать" тип контента (защита от маскировки скриптов под картинки)
    response.headers["X-Content-Type-Options"] = "nosniff"

    # Запрещает встраивать ваш сайт в <iframe> на других доменах (защита от Clickjacking)
    response.headers["X-Frame-Options"] = "DENY"

    # Включает встроенную в браузер фильтрацию XSS
    response.headers["X-XSS-Protection"] = "1; mode=block"

    # Строгий HTTPS (HSTS) - раскомментируйте, если у вас настроен SSL-сертификат
    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response

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