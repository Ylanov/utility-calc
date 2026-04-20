import time
import os
import socket

from prometheus_client import Counter, Histogram, start_http_server
from celery.signals import task_prerun, task_postrun, task_failure
from celery import Celery
import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from app.core.config import settings
from celery.schedules import crontab

# =====================================================
# SENTRY
# =====================================================

if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        integrations=[CeleryIntegration()],
    )

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
}

# ИМПОРТЫ ЗАДАЧ
celery.conf.imports = [
    "app.modules.utility.tasks",
    # Если в будущем появятся фоновые задачи у Арсенала или ГСМ,
    # нужно будет создать там файлы tasks.py и раскомментировать строки ниже:
    # "app.modules.arsenal.tasks",
    # "app.modules.gsm.tasks"
]

# =====================================================
# PROMETHEUS METRICS
# =====================================================

HOSTNAME = socket.gethostname()

TASK_COUNT = Counter(
    "celery_task_total",
    "Total number of Celery tasks", ["task_name", "status", "worker"]
)

TASK_TIME = Histogram(
    "celery_task_duration_seconds",
    "Time spent processing tasks",
    ["task_name", "worker"]
)

_task_start_time = {}


# =====================================================
# SIGNALS
# =====================================================

# Celery 5.x всегда передаёт в connect-обработчики аргументы как kwargs,
# не как positional. Старая сигнатура `task_failure_handler(task_id, exception, task, ...)`
# работала только пока Celery дублировал их в позиционные. После апгрейда стало:
#   TypeError: task_failure_handler() missing 1 required positional argument: 'task'
# Поэтому принимаем всё через kwargs с дефолтами.

@task_prerun.connect
def task_prerun_handler(sender=None, task_id=None, task=None, **kwargs):
    if task_id:
        _task_start_time[task_id] = time.time()


@task_postrun.connect
def task_postrun_handler(sender=None, task_id=None, task=None, **kwargs):
    start_time = _task_start_time.pop(task_id, None) if task_id else None
    if start_time and task is not None:
        duration = time.time() - start_time
        TASK_TIME.labels(task.name, HOSTNAME).observe(duration)
    if task is not None:
        TASK_COUNT.labels(task.name, "success", HOSTNAME).inc()


@task_failure.connect
def task_failure_handler(sender=None, task_id=None, exception=None, **kwargs):
    if task_id:
        _task_start_time.pop(task_id, None)
    # sender — это сам объект Task в Celery 5
    if sender is not None:
        TASK_COUNT.labels(sender.name, "failure", HOSTNAME).inc()


# =====================================================
# METRICS SERVER (SAFE START)
# =====================================================

def start_metrics_server():
    try:
        port = int(os.environ.get("METRICS_PORT", "8001"))
        start_http_server(port)
        print(f"[Metrics] Prometheus metrics started on port {port}")
    except Exception as e:
        print(f"[Metrics] Failed to start metrics server: {e}")


# 🔥 ВКЛЮЧАЕМ ТОЛЬКО ЯВНО
if os.environ.get("ENABLE_METRICS", "false").lower() == "true":
    # Запускаем только в одном процессе (защита от gunicorn/celery fork)
    if os.getpid() == 1:
        start_metrics_server()
