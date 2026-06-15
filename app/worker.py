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
        # ИСПРАВЛЕНИЕ (apr 2026): раньше пути были dotted —
        # "app.modules.utility.tasks.generate_receipt_task". Но Celery routes
        # сравнивают с task.name, а у нас @celery.task(name="generate_receipt_task")
        # переопределяет dotted на короткое. Итог: routes никогда не матчили,
        # heavy-задачи (PDF, импорт долгов) уходили в default queue.
        # Теперь — короткие имена, точно как в @celery.task(name=...).
        #
        # NB: задачи с queue=... в @celery.task декораторе игнорируют этот
        # fallback (start_bulk_receipt_generation, close_period_task — heavy;
        # run_arsenal_analyzer_task — arsenal_gsm_default).
        "generate_receipt_task": {"queue": "heavy"},
        "import_debts_task": {"queue": "heavy"},

        # ВСЕ ОСТАЛЬНЫЕ ЗАДАЧИ (легкие ЖКХ) -> default queue.
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
    # Авто-cleanup outlier readings (нереалистичные суммы > MAX_TOTAL_COST_PER_READING).
    # Раз в сутки в 03:30 — после cleanup_gsheets, чтобы порядок чисток был
    # последовательным. Сбрасывает их в DATA_OVERFLOW_RESET (is_approved=False,
    # total=0) — админ потом разберёт через bell-notifications (категория
    # data_overflow_resets). См. tasks._cleanup_outlier_readings_run.
    "cleanup-outlier-readings-daily": {
        "task": "cleanup_outlier_readings_task",
        "schedule": crontab(minute=30, hour=3),
    },
    # Очистка архива оригинальных xlsx из 1С (DebtImportLog.archive_path).
    # Запускается раз в неделю в воскресенье 03:15. Удаляет файлы старше
    # debt.archive_retention_days (default 730 дней) или log.retention_days
    # (per-log override). Сами DebtImportLog НЕ удаляются — только файл,
    # archive_path обнуляется чтобы UI «Скачать» давал 404 а не битый путь.
    "cleanup-debt-archives-weekly": {
        "task": "cleanup_debt_archives_task",
        "schedule": crontab(minute=15, hour=3, day_of_week=0),
    },
    # Privacy: переписки с админом, начатые с QR-портала, живут 5 дней и
    # авто-удаляются (cleanup_qr_tickets_task). Ежедневно в 03:50.
    "cleanup-qr-tickets-daily": {
        "task": "cleanup_qr_tickets_task",
        "schedule": crontab(minute=50, hour=3),
    },
    # Bug AO: дневная авто-добивка нормативом. Каждый день в 03:45 проходит
    # по периодам, которые закрыты (или давно неактивны), и добавляет
    # reading'и для жильцов без подачи — по стратегии AUTO_NORM_SANCTION /
    # AVG / FALLBACK (см. billing.auto_fill_period_readings).
    # Активный период НЕ трогает (там жильцы ещё могут подать).
    # Можно отключить через analyzer_settings: billing.auto_fill_enabled=false.
    # Время 03:45 — после cleanup-old-readings (03:30), чтобы не пересекаться.
    "auto-fill-missing-readings-daily": {
        "task": "auto_fill_missing_readings_task",
        "schedule": crontab(minute=45, hour=3),
    },
    # Монитор проблем жильцов — каждые 6 часов в :30 (не пересекается с
    # другими задачами). Прогоняет детекторы (не подаёт / долг растёт /
    # формат / замер) и обновляет resident_problems для сигнализации.
    "scan-resident-problems-6h": {
        "task": "scan_resident_problems_task",
        "schedule": crontab(minute=30, hour="*/6"),
    },
    # Авто-перерасчёт расхождений активного периода — каждые 6ч в :45 (после
    # scan :30). Безопасные drift фиксит сам, опасные/повторные → сигнал.
    "auto-recalc-drift-6h": {
        "task": "auto_recalc_drift_task",
        "schedule": crontab(minute=45, hour="*/6"),
    },
}

# ИМПОРТЫ ЗАДАЧ
celery.conf.imports = [
    "app.modules.utility.tasks",
    # Если в будущем появятся фоновые задачи у Арсенала, нужно создать
    # tasks.py и раскомментировать строку ниже:
    # "app.modules.arsenal.tasks",
]

# Наблюдаемость для Celery — через Sentry (CeleryIntegration). Метрики
# task_count / task_duration больше не считаем локально: при наличии 1-2
# админов и небольшом числе фоновых задач Sentry-events достаточно для
# алертов на падения и медленные задачи. Если в будущем понадобятся точные
# тайминги — Sentry Performance их пишет автоматически.

# =====================================================
# КОПИЛКА ОШИБОК (E3-A, 28.05.2026)
# Каждое падение задачи дополнительно сохраняем в БД error_log, чтобы
# админ видел в /api/admin/errors без захода в Sentry.
# =====================================================
from celery.signals import task_failure  # noqa: E402


@task_failure.connect
def _on_task_failure(sender=None, task_id=None, exception=None,
                     args=None, kwargs=None, traceback=None,
                     einfo=None, **_):
    """Перехват любого failure celery-задачи → error_log."""
    import asyncio
    import logging
    import traceback as _tb

    _log = logging.getLogger("celery.task_failure_hook")

    try:
        from app.core.database import AsyncSessionLocal
        from app.core.error_logger import log_error

        task_name = getattr(sender, "name", str(sender)) if sender else "unknown"
        tb_str = None
        if einfo is not None and hasattr(einfo, "traceback"):
            tb_str = str(einfo.traceback)[:50000]
        elif exception is not None:
            tb_str = "".join(
                _tb.format_exception(
                    type(exception), exception, exception.__traceback__
                )
            )[:50000]

        async def _save():
            async with AsyncSessionLocal() as db:
                await log_error(
                    db,
                    source="celery",
                    level="error",
                    exc=exception,
                    traceback_str=tb_str,
                    extra={
                        "task_name": task_name,
                        "task_id": task_id,
                        "args": _truncate_repr(args),
                        "kwargs": _truncate_repr(kwargs),
                    },
                    run_investigation=False,  # для celery URL нет
                )

        # Celery handler — sync; разворачиваем event-loop одноразово.
        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_save())
            loop.close()
        except RuntimeError:
            # Если уже есть running loop (редкий случай для signal-обработчика
            # внутри test runner'а) — лог через sync-обёртку asyncio.run().
            asyncio.run(_save())
    except Exception as e:
        _log.warning("[task_failure_hook] failed to log: %s", e)


def _truncate_repr(value, limit: int = 1000):
    """repr с обрезкой по длине — args/kwargs могут содержать большие dicts."""
    try:
        s = repr(value)
        return s if len(s) <= limit else s[:limit] + f"...[+{len(s)-limit} chars]"
    except Exception:
        return "<unrepr-able>"
