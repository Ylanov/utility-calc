from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.future import select
from app.database import engine, Base, AsyncSessionLocal
from app.models import User, Tariff, BillingPeriod
from app.auth import get_password_hash
from app.routers.system import _apply_migrations_sync

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
app.mount("/", StaticFiles(directory="static", html=True), name="static")


@app.on_event("startup")
async def startup():
    # 1. Создание таблиц (если их нет)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 2. Применение миграций (обновление схемы БД)
    # Это добавит новые колонки (например, period_id), если база была создана старой версией кода.
    try:
        _apply_migrations_sync()
    except Exception as e:
        # Логируем ошибку, но не роняем приложение, так как psql может не быть в PATH локально,
        # или миграции уже применены.
        print(f"Startup migration warning: {e}")

    # 3. Инициализация начальных данных
    async with AsyncSessionLocal() as db:
        # Создание Админа (Бухгалтера)
        admin = await db.execute(select(User).where(User.username == "admin"))
        if not admin.scalars().first():
            db.add(User(username="admin", hashed_password=get_password_hash("admin"), role="accountant"))

        # Создание Тарифов
        tariff = await db.execute(select(Tariff).where(Tariff.id == 1))
        if not tariff.scalars().first():
            db.add(Tariff(id=1, electricity_rate=5.0))

        # Создание первого Периода (если база пустая)
        # Без этого пользователи не смогут подать показания, так как нужен active_period
        period_res = await db.execute(select(BillingPeriod))
        if not period_res.scalars().first():
            print("Creating default billing period...")
            db.add(BillingPeriod(name="Начальный период", is_active=True))

        await db.commit()