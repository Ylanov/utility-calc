# app/core/database.py

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from app.core.config import settings

# =====================================================
# КОНСТАНТЫ
# =====================================================

ISOLATION_LEVEL = "READ COMMITTED"  # ✅ единая точка настройки

# Определение базовых классов моделей
Base = declarative_base()          # ЖКХ
ArsenalBase = declarative_base()   # Арсенал
GsmBase = declarative_base()       # ГСМ

# 🔥 КРИТИЧЕСКИ ВАЖНО ДЛЯ PGBOUNCER
asyncpg_connect_args = {
    "prepared_statement_cache_size": 0,
    "statement_cache_size": 0,
    "command_timeout": 60
}

# =========================================================================
# 1. ЖКХ (Utility DB)
# =========================================================================

engine = create_async_engine(
    settings.DATABASE_URL_ASYNC,
    echo=False,
    future=True,
    poolclass=NullPool,
    isolation_level=ISOLATION_LEVEL,  # ✅
    connect_args=asyncpg_connect_args
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
    poolclass=NullPool,
    isolation_level=ISOLATION_LEVEL  # ✅
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


# =========================================================================
# 2. Arsenal DB
# =========================================================================

arsenal_engine = create_async_engine(
    settings.ARSENAL_DATABASE_URL_ASYNC,
    echo=False,
    future=True,
    poolclass=NullPool,
    isolation_level=ISOLATION_LEVEL,  # ✅
    connect_args=asyncpg_connect_args
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


# =========================================================================
# 3. GSM DB
# =========================================================================

gsm_engine = create_async_engine(
    settings.ARSENAL_DATABASE_URL_ASYNC,
    echo=False,
    future=True,
    poolclass=NullPool,
    isolation_level=ISOLATION_LEVEL,  # ✅
    connect_args=asyncpg_connect_args
)

GsmSessionLocal = sessionmaker(
    bind=gsm_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

async def get_gsm_db():
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