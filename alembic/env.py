import asyncio
from logging.config import fileConfig
import os
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Импорты ваших моделей
from app.modules.utility.models import Base
from app.core.config import settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    # Забираем доступы (с фоллбэком на стандартные имена переменных Postgres)
    db_user = os.getenv("POSTGRES_USER", os.getenv("DB_USER", "postgres"))
    db_pass = os.getenv("POSTGRES_PASSWORD", os.getenv("DB_PASS", ""))

    # ❗ ВАЖНО: подключаемся напрямую к postgres (по умолчанию 'db')
    db_host = os.getenv("DB_HOST_DIRECT", "db")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("POSTGRES_DB", os.getenv("DB_NAME", "utility_db"))

    # Драйвер обязательно должен быть +asyncpg для асинхронного движка
    return f"postgresql+asyncpg://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}"


def run_migrations_offline() -> None:
    url = os.getenv("DATABASE_URL") or get_database_url()

    context.configure(
        url=url,
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

    database_url = os.getenv("DATABASE_URL") or get_database_url()

    configuration["sqlalchemy.url"] = database_url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


# ======================================================================
# ❗ САМАЯ ВАЖНАЯ ЧАСТЬ, КОТОРОЙ НЕ БЫЛО: БЛОК ЗАПУСКА МИГРАЦИЙ
# Без этого блока Alembic просто прочитает файл и ничего не сделает
# ======================================================================
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())