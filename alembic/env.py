import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# --- ИМПОРТЫ ИЗ ТВОЕГО ПРОЕКТА ---
# Импортируем Base, чтобы Alembic видел твои модели (User, MeterReading и т.д.)
from app.models import Base
# Импортируем настройки, чтобы взять URL базы данных
from app.config import settings

# это объект конфигурации Alembic, который дает доступ к значениям из .ini файла
config = context.config

# Настройка логирования
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# --- ВАЖНО: Связываем метаданные моделей ---
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Запуск миграций в 'offline' режиме (без подключения к БД)."""
    # Используем синхронный URL для оффлайн режима (если нужно)
    url = settings.DATABASE_URL_SYNC
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
    """Запуск миграций в 'online' режиме (с подключением к БД)."""

    # Мы подменяем URL из alembic.ini на наш URL из config.py
    configuration = config.get_section(config.config_ini_section)
    configuration["sqlalchemy.url"] = settings.DATABASE_URL_ASYNC

    # Создаем асинхронный движок только для миграций
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        # Alembic работает синхронно внутри асинхронного потока
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())