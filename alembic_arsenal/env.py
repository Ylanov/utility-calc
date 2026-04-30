import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core.config import settings

# Импортируем модели Арсенала.
# Модуль ГСМ удалён из проекта (apr 2026) — раньше тут был ещё импорт
# GsmBase из app.modules.gsm.models. Существующие миграции, создававшие
# gsm_*-таблицы, остаются в истории; отдельная cleanup-миграция дропает
# таблицы при следующем upgrade head.
from app.modules.arsenal.models import ArsenalBase

config = context.config

# Настройка логирования
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = ArsenalBase.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode."""
    # Используем синхронный URL для оффлайн режима
    url = settings.ARSENAL_DATABASE_URL_SYNC
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection):
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online():
    """Run migrations in 'online' mode."""
    configuration = config.get_section(config.config_ini_section)

    # Используем асинхронный URL Арсенала из конфига приложения
    configuration["sqlalchemy.url"] = settings.ARSENAL_DATABASE_URL_ASYNC

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
