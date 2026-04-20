# app/core/config.py

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
    # ИСПРАВЛЕНИЕ P0: Добавлено отдельное имя БД для ГСМ.
    # Ранее GSM engine использовал ARSENAL_DATABASE_URL_ASYNC — все данные ГСМ
    # читались и писались в базу Арсенала. Если ГСМ живёт в той же БД что и Арсенал,
    # задайте GSM_DB_NAME = "arsenal_db" в .env. По умолчанию — отдельная БД.
    GSM_DB_NAME: str = "gsm_db"

    SECRET_KEY: str

    ENCRYPTION_KEY: str = "gR8g_2t9R2YwO9yZ0qEa7L_M4-c8Kx2mJ1rYvW4PZ7o="
    TELEGRAM_BOT_TOKEN: Optional[str] = None

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

    # ИСПРАВЛЕНИЕ P0: Отдельные URL для ГСМ
    @property
    def GSM_DATABASE_URL_ASYNC(self) -> str:
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASS}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.GSM_DB_NAME}"
        )

    @property
    def GSM_DATABASE_URL_SYNC(self) -> str:
        return (
            f"postgresql://{self.DB_USER}:{self.DB_PASS}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.GSM_DB_NAME}"
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
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()

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

if settings.ENVIRONMENT == "production":
    # CRITICAL — без них приложение реально небезопасно, отказываем в старте.
    if not settings.SECRET_KEY or len(settings.SECRET_KEY) < 32:
        raise RuntimeError(
            "В production требуется безопасный SECRET_KEY (не менее 32 символов)"
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

    # S3 — только WARNING. Смена MinIO-ключей — отдельная процедура:
    # надо создать нового пользователя через MinIO Console, а не менять
    # просто в .env (иначе MinIO упадёт с несовпадением root_password).
    # Логируем и продолжаем — пусть админ смигрирует когда удобно.
    if (
        settings.S3_ACCESS_KEY == _DEFAULT_S3_ACCESS
        or settings.S3_SECRET_KEY == _DEFAULT_S3_SECRET
    ):
        import logging as _log
        _log.getLogger(__name__).warning(
            "SECURITY: S3_ACCESS_KEY/S3_SECRET_KEY используют значения по умолчанию "
            "(minioadmin). Это безопасно только если MinIO не пробрасывается во "
            "внешнюю сеть. Рекомендуется сменить через MinIO Console."
        )