from sqlalchemy.ext.asyncio import (
    AsyncSession,
    create_async_engine
)
from sqlalchemy.orm import (
    sessionmaker,
    declarative_base
)
from app.config import settings

# -------------------------------------------------
# СОЗДАНИЕ ASYNC ENGINE
# -------------------------------------------------

engine = create_async_engine(
    settings.DATABASE_URL_ASYNC,  # Берем из конфига

    echo=False,

    # Проверяет соединение перед использованием
    pool_pre_ping=True,

    # Ограничение пула (стабильность под нагрузкой в 1000 юзеров)
    pool_size=20,
    max_overflow=10,

    # Таймаут ожидания соединения
    pool_timeout=30
)


# -------------------------------------------------
# ФАБРИКА СЕССИЙ
# -------------------------------------------------

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)


# -------------------------------------------------
# БАЗОВАЯ МОДЕЛЬ SQLALCHEMY
# -------------------------------------------------

Base = declarative_base()


# -------------------------------------------------
# DEPENDENCY ДЛЯ FASTAPI
# -------------------------------------------------

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()