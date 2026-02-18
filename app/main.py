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
from app.arsenal import auth as arsenal_auth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Utility Calculator",
    version="1.0.0"
)

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

app.mount(
    "/static",
    StaticFiles(directory="static",html=False),
    name="static"
)

@app.on_event("startup")
async def startup_event():
    logger.info("Starting application worker...")
    try:
        redis_client=redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True
        )
        await FastAPILimiter.init(redis_client)
        logger.info("Redis connected for rate limiting.")
    except Exception as error:
        logger.warning(f"Redis unavailable, rate limiting disabled: {error}")
    logger.info("Application worker startup complete.")
