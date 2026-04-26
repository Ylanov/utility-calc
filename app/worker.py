from celery import Celery
from app.core.config import settings
from celery.schedules import crontab

# =====================================================
# SENTRY
# =====================================================
# Тот же набор интеграций, что в web (см. app/core/sentry_init.py).
# Tag component="worker" позволит фильтровать события Celery от HTTP
# в Sentry-дашборде.
from app.core.sentry_init import setup_sentry
setup_sentry(component="worker")

# =====================================================
# CELERY INIT
# =====================================================

celery = Celery(
    "app",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Europe/Moscow",
    enable_utc=True,

    worker_concurrency=settings.CELERY_WORKER_CONCURRENCY,
    worker_prefetch_multiplier=1,

    task_acks_late=True,
    task_reject_on_worker_lost=True,

    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    result_expires=settings.CELERY_RESULT_EXPIRES,

    broker_connection_retry_on_startup=True,

    # =====================================================
    # MEMORY SAFETY
    # =====================================================
    worker_max_tasks_per_child=500,
    worker_max_memory_per_child=1500000,

    # =====================================================
    # QUEUES (РОУТИНГ ЗАДАЧ МЕЖДУ КОНТЕЙНЕРАМИ)
    # =====================================================
    task_default_queue="default",
    task_routes={
        # ТЯЖЕЛЫЕ ЗАДАЧИ ЖКХ (PDF, ZIP, 1C) -> Уходят в контейнер worker_jkh_heavy
        "app.modules.utility.tasks.generate_receipt_task": {"queue": "heavy"},
        "app.modules.utility.tasks.create_zip_archive_task": {"queue": "heavy"},
        "app.modules.utility.tasks.start_bulk_receipt_generation": {"queue": "heavy"},
        "app.modules.utility.tasks.import_debts_task": {"queue": "heavy"},
        "app.modules.utility.tasks.close_period_task": {"queue": "heavy"},

        # ЗАДАЧИ АРСЕНАЛА И ГСМ -> Уходят в изолированный контейнер worker_arsenal_gsm
        "app.modules.arsenal.tasks.*": {"queue": "arsenal_gsm_default"},
        "app.modules.gsm.tasks.*": {"queue": "arsenal_gsm_default"},

        # ВСЕ ОСТАЛЬНЫЕ ЗАДАЧИ (Легкие задачи ЖКХ) -> Уходят в worker_jkh_default
        "*": {"queue": "default"},
    }
)

# =====================================================
# CELERY BEAT
# =====================================================

celery.conf.beat_schedule = {
    "check-submission-period-daily": {
        "task": "check_auto_period_task",
        "schedule": crontab(minute=5, hour=0),
    },
    # Активация запланированных тарифов — каждый день в 00:01 (раньше check_auto_period)
    "activate-scheduled-tariffs-daily": {
        "task": "activate_scheduled_tariffs_task",
        "schedule": crontab(minute=1, hour=0),
    },
    # Синхронизация показаний из Google Sheets — каждые N минут.
    # Если GSHEETS_SHEET_ID не задан в .env, задача внутри выйдет сразу.
    # Интервал настраивается через GSHEETS_SYNC_INTERVAL_MINUTES.
    "sync-gsheets-periodic": {
        "task": "sync_gsheets_task",
        "schedule": (
            crontab(minute=f"*/{settings.GSHEETS_SYNC_INTERVAL_MINUTES}")
            if settings.GSHEETS_SYNC_INTERVAL_MINUTES and settings.GSHEETS_SYNC_INTERVAL_MINUTES > 0
            else crontab(minute=0, hour=0, day_of_month="31", month_of_year="2")  # никогда
        ),
    },
    # Анализатор арсенала: раз в час проверяет данные на дубли / застой /
    # фрод-паттерны. Результат попадает в arsenal_anomaly_flags.
    "arsenal-analyzer-hourly": {
        "task": "run_arsenal_analyzer_task",
        "schedule": crontab(minute=15),
    },
    # Автоочистка старых завершённых строк импорта из Google Sheets.
    # Запускается раз в сутки в 03:00 — спокойное время, нагрузки нет.
    # Удаляет approved/auto_approved/rejected старше GSHEETS_CLEANUP_DAYS
    # (дефолт 365 дней). pending/conflict/unmatched не трогает — их ждут
    # админы в буфере. Если GSHEETS_CLEANUP_DAYS=0 — задача выходит сразу.
    "cleanup-gsheets-old-rows-daily": {
        "task": "cleanup_gsheets_old_rows_task",
        "schedule": crontab(minute=0, hour=3),
    },
}

# ИМПОРТЫ ЗАДАЧ
celery.conf.imports = [
    "app.modules.utility.tasks",
    # Если в будущем появятся фоновые задачи у Арсенала или ГСМ,
    # нужно будет создать там файлы tasks.py и раскомментировать строки ниже:
    # "app.modules.arsenal.tasks",
    # "app.modules.gsm.tasks"
]

# Наблюдаемость для Celery — через Sentry (CeleryIntegration). Метрики
# task_count / task_duration больше не считаем локально: при наличии 1-2
# админов и небольшом числе фоновых задач Sentry-events достаточно для
# алертов на падения и медленные задачи. Если в будущем понадобятся точные
# тайминги — Sentry Performance их пишет автоматически.
