import logging
import sentry_sdk
import redis.asyncio as redis
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi_limiter import FastAPILimiter
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend

from sqlalchemy.dialects.postgresql import insert
from passlib.context import CryptContext

from app.core.config import settings
from app.core.database import ArsenalSessionLocal, GsmSessionLocal
from app.modules.arsenal.models import ArsenalUser
from app.modules.gsm.models import GsmUser
from app.modules.utility.routers import settings as settings_router

# --- Импорт Роутеров ЖКХ ---
from app.modules.utility.routers import (
    admin_periods, client_readings, admin_reports, auth_routes,
    tariffs, admin_readings, users, admin_adjustments, admin_user_ops, financier
)
from app.modules.telegram import telegram_app

# --- Импорт Роутеров Арсенала ---
from app.modules.arsenal.routers import (
    system as arsenal_system,
    objects as arsenal_objects,
    nomenclature as arsenal_nomenclature,
    documents as arsenal_documents,
    users as arsenal_users_router,
)

# Если reports.py, routes.py и auth.py остались в корне arsenal, оставьте их отдельно:
from app.modules.arsenal import (
    reports as arsenal_reports,
    routes as arsenal_routes,
    auth as arsenal_auth
)

# --- Импорт Роутеров ГСМ ---
from app.modules.gsm import (
    routes as gsm_routes,
    auth as gsm_auth,
    reports as gsm_reports
)

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Контекст хеширования паролей
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# Инициализация Sentry (мониторинг ошибок)
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
    )


# =====================================================================
# LIFESPAN (Замена устаревшему @app.on_event("startup"))
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Управление жизненным циклом приложения:
    1. Подключение к Redis (Кэш + Лимитеры)
    2. Проверка/Создание администраторов в БД
    """
    logger.info("🚀 Starting application initialization...")

    # 1. Подключение к Redis
    try:
        redis_client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True
        )
        # Инициализация Rate Limiter (защита от DDOS)
        await FastAPILimiter.init(redis_client)
        # Инициализация Cache (для KPI и справочников)
        FastAPICache.init(RedisBackend(redis_client), prefix="fastapi-cache")
        logger.info("✅ Redis connected successfully.")
    except Exception as error:
        logger.error(f"❌ Redis connection failed: {error}")
        # Не прерываем запуск, но кэш работать не будет

    # 2. Создание дефолтных админов (Idempotent check)
    try:
        await ensure_admin_exists(ArsenalSessionLocal, ArsenalUser)
        logger.info("✅ Arsenal admin ensured.")
    except Exception as e:
        logger.error(f"⚠️ Failed to ensure Arsenal admin: {e}")

    try:
        await ensure_admin_exists(GsmSessionLocal, GsmUser)
        logger.info("✅ GSM admin ensured.")
    except Exception as e:
        logger.error(f"⚠️ Failed to ensure GSM admin: {e}")

    yield  # Приложение работает здесь

    # Shutdown logic (если нужно закрыть соединения)
    logger.info("🛑 Application shutdown.")


# =====================================================================
# Инициализация FastAPI
# =====================================================================
app = FastAPI(
    title="Utility Calculator & Arsenal & GSM",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None
)

# =====================================================================
# НАСТРОЙКА БЕЗОПАСНОСТИ (CORS & Headers)
# =====================================================================

# Получаем список разрешенных доменов.
# Если в config.py нет ALLOWED_ORIGINS, используем безопасный дефолт для локальной разработки.
# В ПРОДАКШЕНЕ ОБЯЗАТЕЛЬНО ЗАДАЙТЕ ALLOWED_ORIGINS В .env!
allowed_origins: List[str] = getattr(settings, "ALLOWED_ORIGINS", [])
if not allowed_origins:
    logger.warning("⚠️ ALLOWED_ORIGINS not set. Defaulting to localhost.")
    allowed_origins = ["http://localhost", "http://localhost:8000", "http://127.0.0.1:8000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With", "Accept"],
)


# Middleware безопасности (HSTS, XSS, Clickjacking)
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    # Защита от сниффинга MIME-типов
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Защита от встраивания в iframe (Clickjacking) - разрешаем только с того же домена или запрещаем вовсе
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # Базовая защита от XSS (для старых браузеров)
    response.headers["X-XSS-Protection"] = "1; mode=block"

    # HSTS (Strict-Transport-Security) только для продакшена
    if settings.ENVIRONMENT == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    return response


# =====================================================================
# Вспомогательная функция создания админа
# =====================================================================
async def ensure_admin_exists(session_factory, user_model):
    """
    Создает пользователя admin/admin, если его нет.
    Использует UPSERT для идемпотентности.
    """
    default_password = "admin"
    hashed_pw = pwd_context.hash(default_password)

    async with session_factory() as db:
        # PostgreSQL: INSERT ... ON CONFLICT DO UPDATE
        stmt = insert(user_model).values(
            username="admin",
            hashed_password=hashed_pw,
            role="admin",
            object_id=None
        ).on_conflict_do_update(
            index_elements=["username"],
            set_={
                "role": "admin"
            }
        )
        await db.execute(stmt)
        await db.commit()


# =====================================================================
# Подключение Роутеров (Routes)
# =====================================================================

# --- ЖКХ ---
app.include_router(auth_routes.router)
app.include_router(users.router)
app.include_router(tariffs.router)
app.include_router(client_readings.router)
app.include_router(admin_readings.router)
app.include_router(admin_periods.router)
app.include_router(admin_reports.router)
app.include_router(admin_user_ops.router)
app.include_router(admin_adjustments.router)
app.include_router(financier.router)
app.include_router(telegram_app.router)
app.include_router(settings_router.router)

# --- АРСЕНАЛ (Weaponry) ---
app.include_router(arsenal_auth.router)  # Авторизация
app.include_router(arsenal_system.router, prefix="/api/arsenal")  # KPI, Импорт
app.include_router(arsenal_objects.router, prefix="/api/arsenal")  # Объекты, Баланс
app.include_router(arsenal_nomenclature.router, prefix="/api/arsenal")  # Справочник
app.include_router(arsenal_documents.router, prefix="/api/arsenal")  # Документы
app.include_router(arsenal_users_router.router, prefix="/api/arsenal")  # Пользователи
app.include_router(arsenal_reports.router)  # Отчеты (Timeline)

app.include_router(arsenal_reports.router, prefix="/api/arsenal")

# Для обратной совместимости старых путей (если были)
app.include_router(arsenal_routes.router)

# --- ГСМ (Fuel) ---
app.include_router(gsm_routes.router)
app.include_router(gsm_auth.router)
app.include_router(gsm_reports.router)

# --- Статика ---
app.mount("/static", StaticFiles(directory="static", html=False), name="static")