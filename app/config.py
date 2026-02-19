from pydantic_settings import BaseSettings
from pydantic import ConfigDict, field_validator
from typing import Literal, Optional


class Settings(BaseSettings):
    DB_USER: str = "postgres"
    DB_PASS: str = "postgres"
    DB_HOST: str = "db" # По умолчанию db, в docker-compose переопределим на pgbouncer
    DB_PORT: str = "5432"
    DB_NAME: str = "utility_db"
    ARSENAL_DB_NAME: str = "arsenal_db"

    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    REDIS_URL: str = "redis://redis:6379/0"

    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False

    # Настройки пула SQLAlchemy (для локального подключения)
    # При использовании PgBouncer эти значения в приложении можно уменьшить,
    # так как пулингом занимается сам PgBouncer.
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800

    CELERY_WORKER_CONCURRENCY: int = 4
    CELERY_TASK_TIME_LIMIT: int = 300
    CELERY_RESULT_EXPIRES: int = 3600

    # Sentry
    SENTRY_DSN: Optional[str] = None
    SENTRY_TRACES_SAMPLE_RATE: float = 0.1 # 10% транзакций для трейсинга

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

    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()


if settings.ENVIRONMENT == "production":
    if settings.SECRET_KEY.lower().startswith("default"):
        raise RuntimeError("В production запрещено использовать default SECRET_KEY")
    if settings.DB_USER == "postgres" and settings.DB_PASS == "postgres":
        # Это предупреждение можно убрать, если PgBouncer использует те же креды внутри сети
        pass
    if not settings.REDIS_URL.startswith("redis://"):
        raise RuntimeError("Некорректный REDIS_URL для production")