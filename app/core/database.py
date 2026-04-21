# app/core/database.py

import logging
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool, QueuePool
from app.core.config import settings

logger = logging.getLogger(__name__)

# =====================================================
# КОНСТАНТЫ
# =====================================================

ISOLATION_LEVEL = "READ COMMITTED"

# Определение базовых классов моделей
Base = declarative_base()          # ЖКХ
ArsenalBase = declarative_base()   # Арсенал
GsmBase = declarative_base()       # ГСМ

# Аргументы подключения asyncpg (критично для PgBouncer)
asyncpg_connect_args = {
    "prepared_statement_cache_size": 0,
    "statement_cache_size": 0,
    "command_timeout": 60
}


# =====================================================
# ИСПРАВЛЕНИЕ P1: Выбор стратегии пулинга
# =====================================================
# С PgBouncer: NullPool в приложении, пул управляется PgBouncer.
# Без PgBouncer: QueuePool со встроенным пулом SQLAlchemy.

def _get_async_pool_kwargs() -> dict:
    """Возвращает kwargs для create_async_engine в зависимости от наличия PgBouncer."""
    if settings.USE_PGBOUNCER:
        return {"poolclass": NullPool}
    return {
        "poolclass": QueuePool,
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
        "pool_recycle": settings.DB_POOL_RECYCLE,
        "pool_pre_ping": True,
    }


def _get_sync_pool_kwargs() -> dict:
    """Для sync engine (Celery): NullPool всегда — воркеры короткоживущие."""
    return {"poolclass": NullPool}


# =========================================================================
# 1. ЖКХ (Utility DB) — Async
# =========================================================================

engine = create_async_engine(
    settings.DATABASE_URL_ASYNC,
    echo=False,
    future=True,
    isolation_level=ISOLATION_LEVEL,
    connect_args=asyncpg_connect_args,
    **_get_async_pool_kwargs()
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False
)

# =========================================================================
# 1b. ЖКХ (Utility DB) — Sync (для Celery)
# =========================================================================

engine_sync = create_engine(
    settings.DATABASE_URL_SYNC,
    echo=False,
    future=True,
    isolation_level=ISOLATION_LEVEL,
    **_get_sync_pool_kwargs()
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
    isolation_level=ISOLATION_LEVEL,
    connect_args=asyncpg_connect_args,
    **_get_async_pool_kwargs()
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


# Sync-движок и session — для Celery-задач (run_arsenal_analyzer и т.д.),
# где async-сессия не нужна / мешает. Ленивая инициализация через factory,
# чтобы не дергать лишний коннект пул при старте процесса.
from sqlalchemy import create_engine as _create_engine_arsenal  # noqa: E402

arsenal_engine_sync = _create_engine_arsenal(
    settings.ARSENAL_DATABASE_URL_SYNC,
    echo=False,
    future=True,
    isolation_level=ISOLATION_LEVEL,
    **_get_sync_pool_kwargs(),
)

ArsenalSessionLocalSync = sessionmaker(
    bind=arsenal_engine_sync,
    autocommit=False,
    autoflush=False,
)


# =========================================================================
# 3. GSM DB
#
# ИСПРАВЛЕНИЕ P0: Ранее gsm_engine использовал settings.ARSENAL_DATABASE_URL_ASYNC.
# Все операции модуля ГСМ (топливо, масла, накладные) читали и писали в базу Арсенала.
# Теперь ГСМ использует собственный URL: settings.GSM_DATABASE_URL_ASYNC.
#
# Если ГСМ и Арсенал живут в одной БД — задайте GSM_DB_NAME=arsenal_db в .env.
# =========================================================================

gsm_engine = create_async_engine(
    settings.GSM_DATABASE_URL_ASYNC,
    echo=False,
    future=True,
    isolation_level=ISOLATION_LEVEL,
    connect_args=asyncpg_connect_args,
    **_get_async_pool_kwargs()
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


# =========================================================================
# Логирование конфигурации при импорте (для отладки)
# =========================================================================
_pool_mode = "NullPool (PgBouncer)" if settings.USE_PGBOUNCER else f"QueuePool (size={settings.DB_POOL_SIZE})"
logger.info(f"Database pool strategy: {_pool_mode}")
logger.info(f"Utility DB: {settings.DB_NAME}")
logger.info(f"Arsenal DB: {settings.ARSENAL_DB_NAME}")
logger.info(f"GSM DB: {settings.GSM_DB_NAME}")