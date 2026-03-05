from celery import Celery
import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from app.core.config import settings
from celery.schedules import crontab

# Инициализация Sentry для мониторинга ошибок в фоновых задачах
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        integrations=[CeleryIntegration()],
    )

# Инициализация приложения Celery
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

    # Настройки конкурентности берем из конфига (для вашего сервера можно ставить 10-20 на worker)
    worker_concurrency=settings.CELERY_WORKER_CONCURRENCY,

    # Предотвращаем "жадность" воркера, чтобы задачи распределялись равномерно
    worker_prefetch_multiplier=1,

    # Подтверждаем выполнение задачи только после завершения (защита от потери при крэше)
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Лимиты времени
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    result_expires=settings.CELERY_RESULT_EXPIRES,

    # Повторная попытка подключения к брокеру при старте
    broker_connection_retry_on_startup=True,

    # =====================================================
    # 🔥 ЗАЩИТА ОТ УТЕЧЕК ПАМЯТИ (ОПТИМИЗИРОВАНО ПОД 144GB RAM) 🔥
    # =====================================================
    # WeasyPrint может течь, но у вас много памяти.
    # Перезапускаем процесс воркера после 200 задач (снижаем оверхед на форки)
    worker_max_tasks_per_child=200,

    # Резервный лимит по памяти: 1 500 000 КБ = ~1.5 ГБ.
    # С вашей RAM это безопасно. Это предотвратит убийство процесса генерации PDF,
    # если отчет получится слишком большим, но спасет сервер от бесконечной утечки.
    worker_max_memory_per_child=1500000,
    # =====================================================

    # --- НАСТРОЙКИ ОЧЕРЕДЕЙ ---
    task_default_queue="default",
    task_routes={
        # Тяжелые задачи (PDF, ZIP, Импорт) отправляем в очередь "heavy"
        "app.modules.utility.tasks.generate_receipt_task": {"queue": "heavy"},
        "app.modules.utility.tasks.create_zip_archive_task": {"queue": "heavy"},
        "app.modules.utility.tasks.start_bulk_receipt_generation": {"queue": "heavy"},
        "app.modules.utility.tasks.import_debts_task": {"queue": "heavy"},
        "app.modules.utility.tasks.close_period_task": {"queue": "heavy"},

        # Легкие и остальные задачи летят в default
        "*": {"queue": "default"},
    }
)

# Периодические задачи (Celery Beat)
celery.conf.beat_schedule = {
    # Проверка каждый день в 00:05: нужно ли открыть новый период или закрыть старый
    'check-submission-period-daily': {
        'task': 'app.modules.utility.tasks.check_auto_period_task',
        'schedule': crontab(minute=5, hour=0),
    },
}

# Автоматический импорт задач из модуля
celery.conf.imports = ["app.modules.utility.tasks"]