# app/core/config.py

import os
from pydantic_settings import BaseSettings
from pydantic import ConfigDict, field_validator
from typing import Literal, Optional


class Settings(BaseSettings):
    DB_USER: str = "postgres"
    DB_PASS: str = "postgres"
    DB_HOST: str = "db"
    DB_PORT: str = "5432"
    DB_NAME: str = "utility_db"
    ARSENAL_DB_NAME: str = "arsenal_db"

    SECRET_KEY: str

    ENCRYPTION_KEY: str = "gR8g_2t9R2YwO9yZ0qEa7L_M4-c8Kx2mJ1rYvW4PZ7o="

    ALGORITHM: str = "HS256"
    # Раньше было 1440 минут (24 часа) — украденный токен работал сутки.
    # Снизили до 2 часов. Для мобильного клиента этого достаточно: он всё
    # равно каждые ~10 минут обращается к /api/readings/state и при 401
    # редиректит на логин.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 120

    REDIS_URL: str = "redis://redis:6379/0"

    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    # Настройки пула SQLAlchemy.
    # При использовании PgBouncer значения можно уменьшить — пулингом занимается PgBouncer,
    # SQLAlchemy работает через NullPool. Без PgBouncer эти значения применяются напрямую.
    # При 5-10к активных пользователей и пике подачи показаний 20-25 числа значения
    # ниже 30/20 приводили к "QueuePool limit of size X overflow Y reached" и 500-ошибкам.
    DB_POOL_SIZE: int = 30
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

    # ИСПРАВЛЕНИЕ P1: Флаг для выбора стратегии пулинга.
    # True = используется PgBouncer (NullPool в приложении, пул на стороне PgBouncer).
    # False = используется встроенный пул SQLAlchemy (pool_size/max_overflow).
    USE_PGBOUNCER: bool = True

    CELERY_WORKER_CONCURRENCY: int = 4
    CELERY_TASK_TIME_LIMIT: int = 300
    CELERY_RESULT_EXPIRES: int = 3600

    # Sentry
    SENTRY_DSN: Optional[str] = None
    SENTRY_TRACES_SAMPLE_RATE: float = 0.1

    # S3 Storage (MinIO)
    S3_ENDPOINT_URL: str = "http://minio:9000"
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_BUCKET_NAME: str = "utility-receipts"
    S3_PUBLIC_URL: str = "https://asy-tk.ru"

    # =========================================
    # Google Sheets integration
    # =========================================
    # ID таблицы (или полный URL — парсер извлечёт ID). Если пусто —
    # эндпоинты возвращают 400, Celery-задача пропускается.
    GSHEETS_SHEET_ID: str = ""
    # gid листа (0 по умолчанию — первый лист).
    GSHEETS_GID: str = "0"
    # Интервал автосинка в минутах (0 = отключить автосинхронизацию).
    GSHEETS_SYNC_INTERVAL_MINUTES: int = 15
    # Сколько дней хранить ЗАВЕРШЁННЫЕ строки импорта (approved / auto_approved /
    # rejected) до автоочистки. Задачи pending/unmatched/conflict не удаляются
    # никогда — они ждут решения админа.
    # Дефолт 365 дней (год). Для 2-летнего хранения выставьте 730.
    # 0 отключает автоочистку полностью.
    GSHEETS_CLEANUP_DAYS: int = 365

    # =========================================
    # ГИС ГМП — авто-подгрузка долгов (мост-расширение gisgmp-bridge)
    # =========================================
    # Статический токен, которым браузерное расширение авторизуется на
    # эндпоинте POST /api/financier/gisgmp/sync. Машинный мост ↔ один
    # эндпоинт, без пользовательского JWT (он короткоживущий и протух бы
    # на фоновом синке раз в 12 ч). Пусто → эндпоинт отвечает 503.
    # Сгенерировать: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
    GISGMP_SYNC_TOKEN: str = ""

    @property
    def DATABASE_URL_ASYNC(self) -> str:
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASS}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def DATABASE_URL_SYNC(self) -> str:
        return (
            f"postgresql://{self.DB_USER}:{self.DB_PASS}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def ARSENAL_DATABASE_URL_ASYNC(self) -> str:
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASS}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.ARSENAL_DB_NAME}"
        )

    @property
    def ARSENAL_DATABASE_URL_SYNC(self) -> str:
        return (
            f"postgresql://{self.DB_USER}:{self.DB_PASS}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.ARSENAL_DB_NAME}"
        )

    @field_validator("SECRET_KEY")
    @classmethod
    def validate_secret_key(cls, value: str) -> str:
        if not value or len(value) < 32:
            raise ValueError("SECRET_KEY должен быть не менее 32 символов")
        return value

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def validate_encryption_key(cls, value: str) -> str:
        if not value or len(value) < 40:
            raise ValueError("ENCRYPTION_KEY должен быть корректным ключом Fernet (не менее 43 символов)")
        return value

    model_config = ConfigDict(
        # Кортеж: pydantic-settings читает файлы по очереди, последний перекрывает.
        # Локальная разработка кладёт .env.local рядом с прод-`.env`, чтобы
        # ENVIRONMENT/SENTRY_DSN/прочие dev-настройки переопределили прод
        # без правки самого `.env` (который уезжает в Docker на сервер).
        # На сервере `.env.local` отсутствует — кортеж тихо пропускает его.
        #
        # ВАЖНО (security): если на проде случайно положить `.env.local`
        # (например забыли убрать после dev-тестов), он переопределит
        # `.env` и может выключить production-режим (ENVIRONMENT=development
        # → seed admin/admin, DEBUG=true и т.п.). Защита ниже.
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()


# =====================================================
# ЗАЩИТА ОТ СЛУЧАЙНОГО .env.local НА ПРОДЕ
# =====================================================
# Если ENVIRONMENT=production, но .env.local физически лежит на диске —
# это аномалия (его быть не должно). Падаем с ошибкой при старте, чтобы
# админ его убрал, вместо тихого dev-режима на проде.
if settings.ENVIRONMENT == "production" and os.path.isfile(".env.local"):
    raise RuntimeError(
        "Security: на production-сервере обнаружен .env.local. "
        "Этот файл предназначен только для dev — удалите его. "
        "Все настройки prod должны быть в .env."
    )

# =====================================================================
# ВАЛИДАЦИЯ НА СТАРТЕ (production)
#
# Раньше значения ENCRYPTION_KEY, S3_ACCESS_KEY, S3_SECRET_KEY имели default'ы
# прямо в коде ("gR8g_...", "minioadmin"). Если админ забывал переопределить
# их в .env — production поднимался со встроенными значениями, и любой,
# кто видел исходный код репозитория, мог расшифровать все TOTP-секреты
# и логиниться в MinIO.
#
# Теперь приложение отказывается стартовать в production, если ключевые
# секреты совпадают со значениями по умолчанию.
# =====================================================================
_DEFAULT_ENCRYPTION_KEY = "gR8g_2t9R2YwO9yZ0qEa7L_M4-c8Kx2mJ1rYvW4PZ7o="
_DEFAULT_S3_ACCESS = "minioadmin"
_DEFAULT_S3_SECRET = "minioadmin"
# Плейсхолдеры SECRET_KEY из репозитория/доков — проходят проверку длины (>=32),
# но являются ПУБЛИЧНЫМИ (видны любому, кто читал репо) → в production запрещены.
_INSECURE_SECRET_KEYS = {
    "long_random_string_for_security_tokens",
    "local-dev-secret-key-change-in-production-min-32-chars",
}

if settings.ENVIRONMENT == "production":
    # CRITICAL — без них приложение реально небезопасно, отказываем в старте.
    if not settings.SECRET_KEY or len(settings.SECRET_KEY) < 32:
        raise RuntimeError(
            "В production требуется безопасный SECRET_KEY (не менее 32 символов)"
        )

    if settings.SECRET_KEY in _INSECURE_SECRET_KEYS:
        raise RuntimeError(
            "В production SECRET_KEY не должен равняться плейсхолдеру из репозитория. "
            "Сгенерируйте новый: `openssl rand -hex 32` и пропишите в .env."
        )

    if not settings.ENCRYPTION_KEY:
        raise RuntimeError("В production требуется ENCRYPTION_KEY.")

    if settings.ENCRYPTION_KEY == _DEFAULT_ENCRYPTION_KEY:
        raise RuntimeError(
            "В production ENCRYPTION_KEY не должен равняться значению по умолчанию. "
            "Сгенерируйте новый: `python -c 'from cryptography.fernet import Fernet;"
            " print(Fernet.generate_key().decode())'`"
        )

    if not settings.DB_PASS or settings.DB_PASS == "postgres":
        raise RuntimeError(
            "В production DB_PASS не может быть пустым или 'postgres'."
        )

    # S3/MinIO: в production дефолты — это hard-fail.
    #
    # Раньше тут было только warning: мол, «MinIO не пробрасывается наружу,
    # значит дефолтные ключи безопасны». На практике docker-compose.prod.yml
    # публикует MinIO-порты 9000/9001, а watchtower с docker.sock расширяет
    # blast radius — один дефолт превращается в полный захват storage.
    #
    # Выход — hard-fail с подсказкой. Это ломает только те деплои, где
    # уже стоит minioadmin/minioadmin, и намеренно: такое нельзя оставлять.
    # Эскейп-хатч: SKIP_S3_DEFAULT_CHECK=1 — на случай перехода или локального
    # prod-like окружения без наружного MinIO.
    if (
        settings.S3_ACCESS_KEY == _DEFAULT_S3_ACCESS
        or settings.S3_SECRET_KEY == _DEFAULT_S3_SECRET
    ):
        import os as _os
        if _os.environ.get("SKIP_S3_DEFAULT_CHECK") == "1":
            import logging as _log
            _log.getLogger(__name__).warning(
                "SECURITY: используются дефолтные S3 ключи (minioadmin). "
                "SKIP_S3_DEFAULT_CHECK=1 — проверка отключена вручную."
            )
        else:
            raise RuntimeError(
                "SECURITY: S3_ACCESS_KEY/S3_SECRET_KEY равны дефолту (minioadmin). "
                "В production это недопустимо: MinIO Console + обновить .env, "
                "иначе установите SKIP_S3_DEFAULT_CHECK=1 осознанно."
            )
