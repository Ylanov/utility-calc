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

# Импортируем роутеры
# Обратите внимание: admin_readings заменен на 4 новых модуля
from app.routers import (
    auth_routes,
    users,
    tariffs,
    client_readings,
    admin_readings,
    admin_periods,
    admin_reports,
    admin_user_ops
)

app = FastAPI()

# Подключаем роутеры
app.include_router(auth_routes.router)
app.include_router(users.router)
app.include_router(tariffs.router)
app.include_router(client_readings.router)

# Подключаем новые разделенные админские роутеры
app.include_router(admin_readings.router)
app.include_router(admin_periods.router)
app.include_router(admin_reports.router)
app.include_router(admin_user_ops.router)

# Статика (Фронтенд)
# Важно: Nginx будет перехватывать статику в продакшене, но этот mount нужен
# для локальной разработки или если Nginx не настроен на статику.
app.mount("/", StaticFiles(directory="static", html=True), name="static")


@app.on_event("startup")
async def startup():
    """
    Действия при запуске приложения:
    1. Создание таблиц БД.
    2. Подключение Redis.
    3. Создание начальных данных.
    """

    # 1. Создание таблиц (если их нет)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. Инициализация Redis для Rate Limiting
    try:
        r = redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
        await FastAPILimiter.init(r)
        print(f"✅ Redis connected successfully at {settings.REDIS_URL}")
    except Exception as e:
        print(f"⚠️ Warning: Redis connection failed. Rate limiting is DISABLED. Error: {e}")

    # Примечание: _apply_migrations_sync была в system.py.
    # Если нужны миграции, лучше использовать команду 'alembic upgrade head' в docker-compose,
    # чем вызывать их из кода python.

    # 3. Инициализация начальных данных
    async with AsyncSessionLocal() as db:
        # Создание Админа
        admin = await db.execute(select(User).where(User.username == "admin"))
        if not admin.scalars().first():
            print("Creating default admin user...")
            db.add(User(username="admin", hashed_password=get_password_hash("admin"), role="accountant"))

        # Создание Тарифов
        tariff = await db.execute(select(Tariff).where(Tariff.id == 1))
        if not tariff.scalars().first():
            print("Creating default tariffs...")
            # Используем Decimal
            db.add(Tariff(id=1, electricity_rate=Decimal("5.0")))

        # Создание первого Периода
        period_res = await db.execute(select(BillingPeriod))
        if not period_res.scalars().first():
            print("Creating default billing period...")
            db.add(BillingPeriod(name="Начальный период", is_active=True))

        await db.commit()