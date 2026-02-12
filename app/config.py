# app/config.py
from pydantic_settings import BaseSettings
from pydantic import ConfigDict


class Settings(BaseSettings):
    # --- Настройки БД ---
    # Эти значения будут использоваться, только если в .env файле нет соответствующих
    DB_USER: str = "postgres"
    DB_PASS: str = "postgres"
    DB_HOST: str = "db"
    DB_PORT: str = "5432"
    DB_NAME: str = "utility_db"

    # --- Безопасность ---
    SECRET_KEY: str = "default_secret_key_for_dev_only"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    # --- Redis ---
    REDIS_URL: str = "redis://redis:6379/0"

    # --- Окружение ---
    ENVIRONMENT: str = "development"

    # --- Свойства для генерации URL ---
    # Они будут использовать значения, загруженные из .env файла
    @property
    def DATABASE_URL_ASYNC(self) -> str:
        return f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASS}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    @property
    def DATABASE_URL_SYNC(self) -> str:
        return f"postgresql://{self.DB_USER}:{self.DB_PASS}@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"

    model_config = ConfigDict(
        # Указываем Pydantic прочитать .env файл
        env_file=".env",
        env_file_encoding="utf-8",
        # Позволяет переопределять переменные (например, в Docker)
        extra="ignore"
    )


# Создаем единственный экземпляр настроек
settings = Settings()

# Выводим сообщение, чтобы убедиться, что проде-ключ загружен
if settings.ENVIRONMENT == "production" and "default" in settings.SECRET_KEY:
    print("WARNING: Running in PRODUCTION mode with a DEFAULT secret key!")