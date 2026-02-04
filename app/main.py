from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.future import select
import redis.asyncio as redis
from fastapi_limiter import FastAPILimiter

from app.database import engine, Base, AsyncSessionLocal
from app.models import User, Tariff, BillingPeriod
from app.auth import get_password_hash
from app.routers.system import _apply_migrations_sync
from app.config import settings

# Импортируем роутеры
from app.routers import auth_routes, users, tariffs, client_readings, admin_readings, system

app = FastAPI()

# Подключаем роутеры
app.include_router(auth_routes.router)
app.include_router(users.router)
app.include_router(tariffs.router)
app.include_router(client_readings.router)
app.include_router(admin_readings.router)
app.include_router(system.router)

# Статика (Фронтенд)
# Важно: Nginx будет перехватывать статику в продакшене, но этот mount нужен
# для локальной разработки или если Nginx не настроен на статику.
app.mount("/", StaticFiles(directory="static", html=True), name="static")


@app.on_event("startup")
async def startup():
    """
    Действия при запуске приложения:
    1. Создание таблиц БД.
    2. Подключение к Redis (для лимитов запросов).
    3. Применение SQL миграций (обновление колонок).
    4. Создание начальных данных (админ, тарифы, период).
    """

    # 1. Создание таблиц (если их нет)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. Инициализация Redis для Rate Limiting
    # Это критически важно для защиты от перебора паролей (Неделя 1)
    try:
        r = redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
        await FastAPILimiter.init(r)
        print(f"✅ Redis connected successfully at {settings.REDIS_URL}")
    except Exception as e:
        print(f"⚠️ Warning: Redis connection failed. Rate limiting is DISABLED. Error: {e}")

    # 3. Применение миграций (обновление схемы БД)
    # Это добавит новые колонки (например, period_id), если база была создана старой версией кода.
    try:
        _apply_migrations_sync()
    except Exception as e:
        # Логируем ошибку, но не роняем приложение, так как psql может не быть в PATH локально
        print(f"Startup migration warning: {e}")

    # 4. Инициализация начальных данных
    async with AsyncSessionLocal() as db:
        # Создание Админа (Бухгалтера)
        admin = await db.execute(select(User).where(User.username == "admin"))
        if not admin.scalars().first():
            print("Creating default admin user...")
            db.add(User(username="admin", hashed_password=get_password_hash("admin"), role="accountant"))

        # Создание Тарифов
        tariff = await db.execute(select(Tariff).where(Tariff.id == 1))
        if not tariff.scalars().first():
            print("Creating default tariffs...")
            db.add(Tariff(id=1, electricity_rate=5.0))

        # Создание первого Периода (если база пустая)
        # Без этого пользователи не смогут подать показания, так как нужен active_period
        period_res = await db.execute(select(BillingPeriod))
        if not period_res.scalars().first():
            print("Creating default billing period...")
            db.add(BillingPeriod(name="Начальный период", is_active=True))

        await db.commit()