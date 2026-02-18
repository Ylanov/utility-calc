import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Импорт моделей и настроек проекта
# Убедитесь, что пути app.models и app.config верны относительно корня проекта
from app.models import Base
from app.config import settings

# Конфигурация Alembic
config = context.config

# Настройка логирования
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Метаданные моделей для автогенерации миграций
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Запуск миграций в 'offline' режиме."""
    url = settings.DATABASE_URL_SYNC  # Для оффлайн режима можно использовать синхронный URL
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Вспомогательная функция для запуска миграций в синхронном контексте."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Запуск миграций в 'online' режиме с использованием AsyncEngine."""

    # Получаем конфигурацию из .ini файла
    configuration = config.get_section(config.config_ini_section)

    # ПОДМЕНА URL: Заменяем URL из alembic.ini на URL из settings.py
    # Это критически важно, чтобы Docker контейнер видел правильный хост 'db'
    configuration["sqlalchemy.url"] = settings.DATABASE_URL_ASYNC

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        # Запускаем синхронную функцию миграции внутри асинхронного соединения
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())