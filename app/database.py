from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import create_engine
from app.config import settings

# --- 1. Конфигурация БД ЖКХ (Utility DB) ---
Base = declarative_base()

# Для PgBouncer в режиме Transaction Pooling нужно отключить prepared statements в asyncpg
asyncpg_connect_args = {
    "prepare_threshold": None,
    "statement_cache_size": 0
}

engine = create_async_engine(
    settings.DATABASE_URL_ASYNC,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=1800,
    isolation_level="READ COMMITTED",
    connect_args=asyncpg_connect_args  # <-- ВАЖНО ДЛЯ PGBOUNCER
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

engine_sync = create_engine(
    settings.DATABASE_URL_SYNC,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_recycle=1800,
    isolation_level="READ COMMITTED"
)

SessionLocalSync = sessionmaker(
    bind=engine_sync,
    autocommit=False,
    autoflush=False
)

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def close_async_engine():
    await engine.dispose()

def close_sync_engine():
    engine_sync.dispose()


# --- 2. Конфигурация БД СТРОБ Арсенал (Arsenal DB) ---
ArsenalBase = declarative_base()

arsenal_engine = create_async_engine(
    settings.ARSENAL_DATABASE_URL_ASYNC,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=1800,
    isolation_level="READ COMMITTED",
    connect_args=asyncpg_connect_args # <-- ТОЖЕ ЧЕРЕЗ PGBOUNCER
)

ArsenalSessionLocal = sessionmaker(
    bind=arsenal_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

async def get_arsenal_db():
    async with ArsenalSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def close_arsenal_engine():
    await arsenal_engine.dispose()