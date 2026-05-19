# app/main.py

import os
import logging
from contextlib import asynccontextmanager
from typing import List

import redis.asyncio as redis

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from fastapi_limiter import FastAPILimiter
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend

from sqlalchemy.dialects.postgresql import insert as pg_insert
from passlib.context import CryptContext

# === CORE ===
from app.core.config import settings
from app.core.database import ArsenalSessionLocal

# === MODELS ===
from app.modules.arsenal.models import ArsenalUser

# === ЖКХ ===
from app.modules.utility.routers import (
    admin_periods,
    client_readings,
    admin_reports,
    auth_routes,
    tariffs,
    admin_readings,
    users,
    rooms,
    admin_adjustments,
    admin_user_ops,
    financier,
    settings as settings_router,
    admin_dashboard,
    admin_initial_readings,
    admin_gsheets,
    admin_analyzer,
    admin_recalc,
    client_certificates,
    me_consent,
    admin_notifications,
    tickets,
    admin_certificates,
    app_releases,
    qr,
)

# === АРСЕНАЛ ===

from app.modules.arsenal import (
    reports as arsenal_reports,
    routes as arsenal_routes,
    auth as arsenal_auth,
)

# =====================================================================
# LOGGING
#
# Структурированный формат с request_id из contextvars. RequestIdFilter
# подкладывает request_id в каждый LogRecord; форматтер показывает его
# в каждой строке. Это даёт сквозную трассировку HTTP-запроса по логам:
# можно `grep <request_id>` и увидеть всю цепочку обработки.
# =====================================================================
from app.core.request_context import RequestIdFilter, JsonFormatter
from app.core.middleware.request_id import RequestIdMiddleware
from app.core.sentry_init import setup_sentry

# JSON-логи в production (агрегация в Loki/CloudWatch/Sentry breadcrumbs),
# текстовые — в development для читаемости в IDE-консоли.
# Переключение через env LOG_FORMAT=text|json (default: json в production).
_LOG_FORMAT_KIND = os.environ.get(
    "LOG_FORMAT",
    "json" if settings.ENVIRONMENT == "production" else "text",
).lower()
_root_handler = logging.StreamHandler()
if _LOG_FORMAT_KIND == "json":
    _root_handler.setFormatter(JsonFormatter())
else:
    _TEXT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] [req:%(request_id)s] %(message)s"
    _root_handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
_root_handler.addFilter(RequestIdFilter())

# Заменяем дефолтные хендлеры root-логгера на наш с фильтром.
logging.basicConfig(
    level=logging.INFO,
    handlers=[_root_handler],
    force=True,  # перебиваем basicConfig, который мог поставить uvicorn
)

# Прикрепляем фильтр к uvicorn-логгерам (access/error) — иначе их строки
# будут без request_id, и трассировка частично теряется.
for _name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
    _lg = logging.getLogger(_name)
    _lg.addFilter(RequestIdFilter())
    for _h in _lg.handlers:
        _h.addFilter(RequestIdFilter())

logger = logging.getLogger(__name__)

# =====================================================================
# SECURITY
# =====================================================================
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

# =====================================================================
# APP MODE
# =====================================================================
APP_MODE = os.environ.get("APP_MODE", "all")

# =====================================================================
# SENTRY
# =====================================================================
# Вызываем ПОСЛЕ logging.basicConfig — чтобы LoggingIntegration
# подхватила настроенные хендлеры/уровни (иначе breadcrumbs не работают).
# Сам импорт setup_sentry — на верху файла, рядом с остальными app.core.*.
setup_sentry(component="web")


# =====================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================================================================

async def ensure_admin_exists_safe(session_local, model, label: str):
    """
    Создаёт администратора если его нет. Не падает при ошибке.

    Раньше пароль был жёстко захардкожен как "admin" — что создавало
    предсказуемую привилегированную учётку на каждом свежем деплое
    Arsenal/GSM. Теперь пароль берётся из ENV ARSENAL_ADMIN_INITIAL_PASSWORD
    (или GSM_ADMIN_INITIAL_PASSWORD для GSM). Если ENV не задан — сидирование
    пропускается: админа создадут руками через db/утилиту миграции.

    Concurrency: INSERT ... ON CONFLICT DO NOTHING — атомарная операция,
    безопасная при параллельном старте нескольких воркеров Gunicorn.
    """
    # Label приходит "Arsenal" | "GSM" — берём env-переменную по шаблону.
    env_key = f"{label.upper()}_ADMIN_INITIAL_PASSWORD"
    initial_password = os.environ.get(env_key)

    if not initial_password:
        logger.warning(
            f"{label}: {env_key} не задан — пропускаем автосоздание admin. "
            "Создайте пользователя вручную или задайте ENV-переменную при деплое."
        )
        return

    if len(initial_password) < 12:
        # Защита от коротких паролей — чтобы нельзя было обойти жёсткий
        # контроль случайно заданным "admin1234".
        logger.error(
            f"{label}: {env_key} слишком короткий (< 12 символов). "
            "Автосоздание admin пропущено."
        )
        return

    try:
        async with session_local() as db:
            hashed_pw = pwd_context.hash(initial_password)

            stmt = (
                pg_insert(model)
                .values(
                    username="admin",
                    hashed_password=hashed_pw,
                    role="admin",
                )
                .on_conflict_do_nothing(index_elements=["username"])
            )

            await db.execute(stmt)
            await db.commit()
            logger.info(f"{label}: admin user ensured (created or already existed)")

    except Exception as e:
        logger.error(f"{label}: Failed to ensure admin exists: {e}", exc_info=True)


# =====================================================================
# LIFESPAN
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting application in mode: {APP_MODE.upper()}")

    try:
        redis_client = redis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        await FastAPILimiter.init(redis_client)
        FastAPICache.init(RedisBackend(redis_client), prefix="fastapi-cache")
        logger.info("Redis connected")
    except Exception as error:
        logger.error(f"Redis connection failed: {error}")
        # ИСПРАВЛЕНИЕ (apr 2026): раньше exception тут просто логгировался
        # и приложение продолжало стартовать. /health возвращал 200, но
        # любой endpoint с RateLimiter (например /api/token)
        # затем падал с 500 — limiter не инициализирован. Это давало
        # обманчивую картину «сервис здоров» при недоступном Redis.
        # В production падаем сразу, чтобы Docker restart-policy перезапустил
        # контейнер; в dev продолжаем стартовать (удобно работать без Redis).
        if settings.ENVIRONMENT == "production":
            raise

    # APP_MODE "arsenal_gsm" сохранён как историческое имя — после удаления
    # модуля GSM (apr 2026) фактически создаём админа только для Arsenal.
    # Переименование значения = каскадные изменения в docker-compose/CI без
    # реального профита.
    if APP_MODE in ("all", "arsenal_gsm"):
        await ensure_admin_exists_safe(ArsenalSessionLocal, ArsenalUser, "Arsenal")

    yield

    logger.info("Application shutdown")


# =====================================================================
# FASTAPI INIT
# =====================================================================
IS_PRODUCTION = settings.ENVIRONMENT == "production"

app = FastAPI(
    title="Utility Calculator & Arsenal & GSM",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None if IS_PRODUCTION else "/docs",
    redoc_url=None,
    openapi_url=None if IS_PRODUCTION else "/openapi.json",
    # orjson быстрее стандартного json.dumps в 2-3 раза на больших списках
    # (readings, отчёты, дашборд). orjson лежит в requirements.txt с самого
    # начала, но FastAPI его не использовал — этот флаг включает.
    default_response_class=ORJSONResponse,
)


# =====================================================================
# HEALTHCHECK ENDPOINT
#
# ИСПРАВЛЕНИЕ: эндпоинт /health отсутствовал — FastAPI возвращал 404,
# CI/CD pipeline и Docker healthcheck падали с кодом 000/404.
#
# ВАЖНО: регистрируется ДО подключения StaticFiles mount.
# StaticFiles монтируется на "/" и перехватывает ВСЕ запросы которые
# не совпали с роутами выше. Если /health зарегистрировать после mount —
# StaticFiles поймает его первым и вернёт 404.
# =====================================================================
@app.get("/health", tags=["System"], include_in_schema=False)
async def health_check():
    """
    Healthcheck для Docker, CI/CD и Nginx.
    Всегда возвращает 200 если сервис поднят.

    Раньше тело ответа раскрывало APP_MODE — это внутренний признак
    развёртывания (jkh/arsenal_gsm/all), публиковать его внешним
    сканерам и ботам не нужно.
    """
    return {"status": "ok"}


# =====================================================================
# MIDDLEWARES
# =====================================================================
# RequestIdMiddleware регистрируется ПЕРВЫМ (значит выполнится последним
# на пути запроса вверх и первым вниз), чтобы request_id был доступен
# во всём остальном middleware-стеке и хендлерах.
app.add_middleware(RequestIdMiddleware)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["asy-tk.ru", "www.asy-tk.ru", "localhost", "127.0.0.1"],
)

allowed_origins: List[str] = getattr(settings, "ALLOWED_ORIGINS", [])

if not allowed_origins:
    logger.warning("ALLOWED_ORIGINS not set, fallback to localhost")
    allowed_origins = [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

    # ===============================================================
    # НАСТРОЙКА CSP (CONTENT SECURITY POLICY)
    # ===============================================================
    # Хардкоринг XSS-защиты (apr 2026):
    # script-src больше не разрешает 'unsafe-inline' для utility-страниц
    # (admin.html, index.html, login.html, portal.html). Это значит, что
    # любая попытка XSS-инъекции через innerHTML с тегом <script>
    # БУДЕТ ЗАБЛОКИРОВАНА БРАУЗЕРОМ.
    # Inline scripts вынесены в external js/portal.js, inline onclick=
    # в admin.html заменён на addEventListener в app.js.
    #
    # style-src 'unsafe-inline' пока остаётся: в HTML много `style="..."`
    # атрибутов, чистка их — отдельная большая задача. Style-src инъекции
    # дают только косметический ущерб (CSS injection), не RCE.
    #
    # Arsenal/GSM используют Tailwind Play CDN, который требует
    # 'unsafe-inline' для script-src — у них отдельная loose CSP.
    strict_csp = (
        "default-src 'self'; "
        "script-src 'self' cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com cdnjs.cloudflare.com; "
        "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com data:; "
        "img-src 'self' data: blob:; "
        "connect-src 'self'; "
        "frame-ancestors 'none';"
    )

    # Политика для модуля Арсенал/ГСМ (разрешаем CDN Tailwind Play)
    # connect-src нужен т.к. Tailwind Play CDN делает fetch-запросы в runtime.
    # 'unsafe-inline' для script-src оставлен — у Tailwind Play в HTML
    # сидит инлайновый <script src="cdn.tailwindcss.com">, и в коде arsenal
    # ещё много inline onclick-handlers (отдельная задача — переписать).
    loose_csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdnjs.cloudflare.com https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com cdnjs.cloudflare.com https://cdn.tailwindcss.com; "
        "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com data:; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' https://cdn.tailwindcss.com; "
        "frame-ancestors 'none';"
    )

    path_lower = request.url.path.lower()
    if "arsenal" in path_lower:
        response.headers["Content-Security-Policy"] = loose_csp
    else:
        response.headers["Content-Security-Policy"] = strict_csp

    return response


@app.middleware("http")
async def no_cache_api_headers(request: Request, call_next):
    response = await call_next(request)

    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, proxy-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    return response


# =====================================================================
# ROUTES — ЖКХ
# =====================================================================
app.include_router(auth_routes.router)
app.include_router(admin_periods.router)
app.include_router(client_readings.router)
app.include_router(admin_reports.router)
app.include_router(tariffs.router)
app.include_router(admin_readings.router)
app.include_router(users.router)
app.include_router(rooms.router)
app.include_router(admin_adjustments.router)
app.include_router(admin_user_ops.router)
app.include_router(financier.router)
app.include_router(settings_router.router)
app.include_router(admin_dashboard.router)

app.include_router(admin_initial_readings.router)
app.include_router(admin_gsheets.router)
app.include_router(admin_analyzer.router)
app.include_router(admin_recalc.router)
app.include_router(client_certificates.router)
app.include_router(me_consent.router)
app.include_router(admin_notifications.router)
app.include_router(tickets.router_client)
app.include_router(tickets.router_admin)
app.include_router(admin_certificates.router)
app.include_router(app_releases.router)
app.include_router(qr.router)

# =====================================================================
# ROUTES — АРСЕНАЛ
# =====================================================================
app.include_router(arsenal_auth.router)
app.include_router(arsenal_routes.router)
app.include_router(arsenal_reports.router)

# =====================================================================
# CRAWLER / WELL-KNOWN 404 STUBS
#
# StaticFiles смонтирован ниже с html=True — это значит, что на любой
# несуществующий путь Starlette отдаёт index.html с кодом 200. Для SPA
# это правильно (deep-link роутер внутри JS берёт путь из location),
# но для ботов-сканеров, crawler'ов и well-known-файлов это создаёт
# путаницу: /robots.txt, /sitemap.xml, /.well-known/security.txt
# возвращают HTML-портал, и любой сканер думает что контент есть.
#
# Регистрируем явные 404 ДО mount'а StaticFiles, чтобы эти пути
# отбивались корректно, а SPA-deep-routing продолжал работать.
# =====================================================================
async def _serve_or_404(filename: str, media_type: str):
    """Отдаём реальный файл из static/ если он лежит на диске, иначе
    честный 404. Смысл: StaticFiles(html=True) на несуществующий путь
    возвращает index.html с 200 OK — crawler'ы видят HTML вместо
    robots.txt/sitemap.xml. Этот хелпер ломает такое поведение."""
    path = os.path.join("static", filename)
    if os.path.isfile(path):
        return FileResponse(path, media_type=media_type)
    raise HTTPException(status_code=404)


@app.get("/robots.txt", include_in_schema=False)
async def _robots_txt():
    return await _serve_or_404("robots.txt", "text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def _sitemap_xml():
    return await _serve_or_404("sitemap.xml", "application/xml")


@app.get("/.well-known/{path:path}", include_in_schema=False)
async def _well_known(path: str):
    """RFC 8615 well-known пути.

    Сейчас обслуживается:
      - security.txt (RFC 9116) — куда сообщать об уязвимостях
    Файлы лежат в static/.well-known/. Если файла нет — честный 404,
    а не SPA-HTML (иначе сканеры подумают что есть валидный документ).
    """
    safe = path.replace("..", "").lstrip("/")
    full_path = os.path.join("static", ".well-known", safe)
    if os.path.isfile(full_path):
        # security.txt по RFC должен отдаваться text/plain.
        media = "text/plain" if safe.endswith(".txt") else "application/octet-stream"
        return FileResponse(full_path, media_type=media)
    raise HTTPException(status_code=404)


# =====================================================================
# КОРНЕВАЯ СТРАНИЦА — лендинг portal.html, а не закрытый ЛК.
# StaticFiles(html=True) по умолчанию отдаёт static/index.html на «/» —
# но index.html это закрытый личный кабинет жильца с meta noindex.
# Из-за этого Яндекс/Google не индексировали корень. Перенаправляем
# вручную ДО mount'а StaticFiles, чтобы «/» = portal.html (открытая
# индексируемая страница со всем SEO).
# =====================================================================
@app.get("/", include_in_schema=False)
async def _root():
    return FileResponse("static/portal.html", media_type="text/html; charset=utf-8")


# =====================================================================
# STATIC FILES
# Монтируется ПОСЛЕДНИМ — перехватывает все запросы которые не
# совпали с роутами FastAPI выше. /health должен быть зарегистрирован
# до этой строки, иначе StaticFiles вернёт 404.
# =====================================================================
app.mount("/", StaticFiles(directory="static", html=True), name="static")
