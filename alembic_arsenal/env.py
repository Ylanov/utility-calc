import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.core.config import settings

# 1. Импортируем модели Арсенала
from app.modules.arsenal.models import ArsenalBase

# 2. Импортируем модели ГСМ
# Это обязательно, чтобы таблицы зарегистрировались в GsmBase.metadata
from app.modules.gsm.models import GsmBase

config = context.config

# Настройка логирования
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 3. ВАЖНО: Объединяем метаданные обеих систем
# Теперь Alembic будет следить и за ArsenalBase, и за GsmBase в одной базе данных
target_metadata = [ArsenalBase.metadata, GsmBase.metadata]


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