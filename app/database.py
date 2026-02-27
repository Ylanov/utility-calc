from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import create_engine
from app.config import settings

# Определение базовых классов моделей
Base = declarative_base()          # ЖКХ
ArsenalBase = declarative_base()   # Арсенал
GsmBase = declarative_base()       # ГСМ (НОВОЕ)

# Для PgBouncer в режиме Transaction Pooling нужно отключить кэширование
# подготовленных выражений в драйвере asyncpg.
asyncpg_connect_args = {
    "statement_cache_size": 0
}

# =========================================================================
# 1. Конфигурация БД ЖКХ (Utility DB)
# =========================================================================

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
    connect_args=asyncpg_connect_args
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

# Синхронный движок (для Celery и тяжелых задач)
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
    """Dependency для ЖКХ"""
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


# =========================================================================
# 2. Конфигурация БД СТРОБ Арсенал (Arsenal DB)
# =========================================================================

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
    connect_args=asyncpg_connect_args
)

ArsenalSessionLocal = sessionmaker(
    bind=arsenal_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

async def get_arsenal_db():
    """Dependency для Арсенала"""
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


# =========================================================================
# 3. Конфигурация БД СТРОБ ГСМ (GSM DB)
# =========================================================================
# Мы используем ту же базу данных, что и для Арсенала (единая база СТРОБ),
# но создаем отдельный engine и sessionmaker для изоляции логики.

gsm_engine = create_async_engine(
    settings.ARSENAL_DATABASE_URL_ASYNC, # Используем ту же БД
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=1800,
    isolation_level="READ COMMITTED",
    connect_args=asyncpg_connect_args
)

GsmSessionLocal = sessionmaker(
    bind=gsm_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

async def get_gsm_db():
    """Dependency для ГСМ"""
    async with GsmSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

async def close_gsm_engine():
    await gsm_engine.dispose()