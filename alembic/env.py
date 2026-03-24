import asyncio
from logging.config import fileConfig
import os
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Импорты ваших моделей
from app.modules.utility.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_async_url() -> str:
    """
    Получает URL базы данных и принудительно устанавливает асинхронный драйвер asyncpg.
    Это решает проблему с GitHub Actions, который передает 'postgresql://...'
    """
    url = os.getenv("DATABASE_URL")

    if not url:
        db_user = os.getenv("POSTGRES_USER", os.getenv("DB_USER", "postgres"))
        db_pass = os.getenv("POSTGRES_PASSWORD", os.getenv("DB_PASS", ""))
        db_host = os.getenv("DB_HOST_DIRECT", os.getenv("DB_HOST", "localhost"))
        db_port = os.getenv("DB_PORT", "5432")
        db_name = os.getenv("POSTGRES_DB", os.getenv("DB_NAME", "utility_db"))
        url = f"postgresql://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"

    # ❗ ПРИНУДИТЕЛЬНО МЕНЯЕМ ДРАЙВЕР НА asyncpg
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    return url


def run_migrations_offline() -> None:
    context.configure(
        url=get_async_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_async_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())