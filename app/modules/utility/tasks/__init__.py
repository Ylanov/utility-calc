"""Celery-задачи ЖКХ-модуля — пакет.

Монолитный tasks.py (~1.8k строк) распилен на модули ЧИСТО МЕХАНИЧЕСКИ:
поведение, имена задач (@celery.task(name=...)) и очереди не менялись.

КРИТИЧНО: Celery регистрирует задачу в момент ИМПОРТА модуля с декоратором.
worker.py подключает пакет через celery.conf.imports =
["app.modules.utility.tasks"] — поэтому __init__ ОБЯЗАН импортировать все
подмодули, иначе воркер не увидит их задачи. Порядок импортов повторяет
порядок секций исходного файла — НЕ пересортировывать.
"""

# Обратная совместимость: sync_db_session/SessionLocalSync/get_sync_db
# исторически импортировались из app.modules.utility.tasks.
from ._shared import SessionLocalSync, get_sync_db, sync_db_session  # noqa: F401

# Порядок = порядок секций монолитного tasks.py. НЕ сортировать!
from .receipts import generate_receipt_task, start_bulk_receipt_generation  # noqa: F401
from .debts import import_debts_task, onec_autopublish_task  # noqa: F401
from .autofill import auto_fill_missing_readings_task  # noqa: F401
from .debt_retention import cleanup_debt_archives_task  # noqa: F401
from .periods import (  # noqa: F401
    activate_scheduled_tariffs_task,
    check_auto_period_task,
    close_period_task,
    run_async_close_period,
)
from .anomalies import detect_anomalies_task, run_arsenal_analyzer_task  # noqa: F401
from .gsheets import sync_gsheets_task  # noqa: F401
from .recalc import recalc_period_apply_task, recalc_period_preview_task  # noqa: F401
from .maintenance import (  # noqa: F401
    auto_recalc_drift_task,
    charge_houses_rent_task,
    cleanup_gsheets_old_rows_task,
    cleanup_outlier_readings_task,
    cleanup_qr_tickets_task,
    scan_resident_problems_task,
    system_health_task,
)

__all__ = [
    "SessionLocalSync",
    "get_sync_db",
    "sync_db_session",
    "generate_receipt_task",
    "start_bulk_receipt_generation",
    "import_debts_task",
    "onec_autopublish_task",
    "auto_fill_missing_readings_task",
    "cleanup_debt_archives_task",
    "run_async_close_period",
    "close_period_task",
    "check_auto_period_task",
    "activate_scheduled_tariffs_task",
    "run_arsenal_analyzer_task",
    "detect_anomalies_task",
    "sync_gsheets_task",
    "recalc_period_preview_task",
    "recalc_period_apply_task",
    "cleanup_gsheets_old_rows_task",
    "cleanup_outlier_readings_task",
    "scan_resident_problems_task",
    "auto_recalc_drift_task",
    "charge_houses_rent_task",
    "cleanup_qr_tickets_task",
    "system_health_task",
]
