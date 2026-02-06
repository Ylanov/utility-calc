from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import create_engine # <--- Добавляем синхронный create_engine
from app.config import settings

# --- ASYNC ENGINE (ДЛЯ FASTAPI) ---
engine = create_async_engine(
    settings.DATABASE_URL_ASYNC,
    echo=False,
    pool_pre_ping=True,
    pool_size=20,
    max_overflow=10,
    pool_timeout=30
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

# --- SYNC ENGINE (ДЛЯ CELERY WORKER) ---
# Celery проще и надежнее работает с синхронным драйвером
engine_sync = create_engine(
    settings.DATABASE_URL_SYNC, # Используем синхронный URL из конфига
    echo=False,
    pool_pre_ping=True
)

SessionLocalSync = sessionmaker(autocommit=False, autoflush=False, bind=engine_sync)

Base = declarative_base()

# Dependency для FastAPI
async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()