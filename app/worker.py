from celery import Celery
import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from app.core.config import settings

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        integrations=[CeleryIntegration()],
    )

celery = Celery(
    "app",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_concurrency=settings.CELERY_WORKER_CONCURRENCY,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    result_expires=settings.CELERY_RESULT_EXPIRES,
    broker_connection_retry_on_startup=True,

    # =====================================================
    # 🔥 ЗАЩИТА ОТ УТЕЧЕК ПАМЯТИ (OOM PROTECTIONS) 🔥
    # =====================================================
    # Перезапускать дочерний процесс после 50 выполненных задач
    worker_max_tasks_per_child=50,

    # Резервный лимит: перезапускать процесс, если он съел больше ~250 МБ RAM
    # Значение указывается в килобайтах (250000 КБ = 250 МБ)
    worker_max_memory_per_child=250000,
    # =====================================================

    # --- НАСТРОЙКИ ОЧЕРЕДЕЙ ---
    task_default_queue="default",
    task_routes={
        # Тяжелые задачи отправляем в отдельную очередь
        # ВАЖНО: Пути обновлены до app.modules.utility.tasks
        "app.modules.utility.tasks.generate_receipt_task": {"queue": "heavy"},
        "app.modules.utility.tasks.create_zip_archive_task": {"queue": "heavy"},
        "app.modules.utility.tasks.start_bulk_receipt_generation": {"queue": "heavy"},
        "app.modules.utility.tasks.import_debts_task": {"queue": "heavy"},
        # Все остальные задачи летят в default
        "*": {"queue": "default"},
    }
)

# ВАЖНО: Указываем Celery, где теперь искать задачи
celery.conf.imports = ["app.modules.utility.tasks"]