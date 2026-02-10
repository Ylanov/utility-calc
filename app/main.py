from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.future import select
import redis.asyncio as redis
from fastapi_limiter import FastAPILimiter
from decimal import Decimal

from app.database import engine, Base, AsyncSessionLocal
from app.models import User, Tariff, BillingPeriod
from app.auth import get_password_hash
from app.config import settings

# Routers
from app.routers import (
    auth_routes,
    users,
    tariffs,
    client_readings,
    admin_readings,
    admin_periods,
    admin_reports,
    admin_user_ops,
    admin_adjustments  # <-- ИЗМЕНЕНИЕ: Импортируем новый роутер
)

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
app.include_router(admin_adjustments.router)  # <-- ИЗМЕНЕНИЕ: Подключаем новый роутер

# -------------------------------------------------
# STATIC
# -------------------------------------------------

# Этот роутер должен быть в конце, чтобы не перехватывать запросы API
app.mount(
    "/",
    StaticFiles(directory="static", html=True),
    name="static"
)

# -------------------------------------------------
# STARTUP
# -------------------------------------------------

@app.on_event("startup")
async def startup_event():
    """
    Startup actions:
    1. Create DB tables
    2. Init Redis limiter
    3. Create base data
    """

    # -------------------------------------------------
    # DB INIT
    # -------------------------------------------------

    async with engine.begin() as conn:
        # Эта команда создаст все таблицы, включая новую "adjustments"
        await conn.run_sync(Base.metadata.create_all)

    # -------------------------------------------------
    # REDIS / LIMITER
    # -------------------------------------------------

    try:
        redis_client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True
        )

        await FastAPILimiter.init(redis_client)

        print(f"✅ Redis connected: {settings.REDIS_URL}")

    except Exception as e:
        print("⚠️ WARNING: Redis unavailable, rate limit disabled")
        print(f"Reason: {e}")

    # -------------------------------------------------
    # DEFAULT DATA
    # -------------------------------------------------

    async with AsyncSessionLocal() as db:

        # --- Admin user ---
        admin_q = await db.execute(
            select(User).where(User.username == "admin")
        )

        if not admin_q.scalars().first():
            print("➡ Creating default admin")

            admin = User(
                username="admin",
                hashed_password=get_password_hash("admin"),
                role="accountant"
            )

            db.add(admin)

        # --- Tariffs ---
        tariff_q = await db.execute(
            select(Tariff).where(Tariff.id == 1)
        )

        if not tariff_q.scalars().first():
            print("➡ Creating default tariffs")

            tariff = Tariff(
                id=1,
                electricity_rate=Decimal("5.0")
            )

            db.add(tariff)

        # --- Billing period ---
        period_q = await db.execute(select(BillingPeriod))

        if not period_q.scalars().first():
            print("➡ Creating default billing period")

            period = BillingPeriod(
                name="Начальный период",
                is_active=True
            )

            db.add(period)

        await db.commit()

        print("✅ Initial data created")