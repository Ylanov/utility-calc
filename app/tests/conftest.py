"""pytest conftest — выставляет минимальные env-переменные ДО импорта
кода приложения. Без этого app/core/config.py падает на pydantic
валидации (SECRET_KEY ≥32 символов и т.д.).

Этот conftest нужен для запуска unit-тестов локально и в CI без
поднятого docker-окружения с реальным .env. Тесты которым нужна
БД/Redis пропускаются маркером @pytest.mark.slow или используют
mock-фикстуры.
"""
import os

# Должно быть ДО import app.* — иначе config.py упадёт на module load.
# Значения только для прохождения валидации pydantic-settings, не используются.
os.environ.setdefault("SECRET_KEY", "test_secret_key_at_least_32_characters_long_value")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("DATABASE_URL_ASYNC", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("POSTGRES_USER", "test")
os.environ.setdefault("POSTGRES_PASSWORD", "test_password")
os.environ.setdefault("POSTGRES_DB", "test_db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("S3_ACCESS_KEY", "test_access_key")
os.environ.setdefault("S3_SECRET_KEY", "test_secret_key_at_least_16chars")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
