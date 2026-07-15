# Синхронизация показаний из Google Sheets (ручной запуск + beat).
# Вербатим-перенос из tasks.py (строки 846-884), поведение 1:1.

from app.worker import celery
from app.core.config import settings

from ._shared import logger, sync_db_session


# =========================================================================
# GOOGLE SHEETS SYNC
# =========================================================================
@celery.task(
    name="sync_gsheets_task",
    queue="default",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 60},
    retry_backoff=True,
    # Первый импорт исторических данных за 2 года — это 50k+ строк с
    # rapidfuzz token_sort_ratio для каждой. На сервере с CPU средней мощности
    # это занимает 8-15 минут. Поэтому ставим time_limit с большим запасом.
    time_limit=1500,        # 25 минут жёсткий
    soft_time_limit=1200,   # 20 минут мягкий — сначала прилетит SoftTimeLimitExceeded
)
def sync_gsheets_task(sheet_id: str = "", gid: str = "", limit: int | None = None):
    """
    Фоновая синхронизация показаний из Google Sheets.

    Запускается:
      - вручную через эндпоинт POST /api/admin/gsheets/sync
      - по расписанию через Celery Beat (см. app/worker.py beat_schedule)

    Если sheet_id не передан — берём из settings.GSHEETS_SHEET_ID.
    Если и там пусто — задача просто логирует и выходит (нет URL).
    """
    from app.modules.utility.services.gsheets_sync import (
        sync_gsheets, extract_sheet_id,
    )

    effective_id = extract_sheet_id(sheet_id or settings.GSHEETS_SHEET_ID or "")
    effective_gid = gid or settings.GSHEETS_GID or "0"

    if not effective_id:
        logger.info("[GSHEETS] GSHEETS_SHEET_ID не задан — автосинк пропущен")
        return {"skipped": True, "reason": "no_sheet_id"}

    with sync_db_session() as db:
        return sync_gsheets(db, effective_id, effective_gid, limit=limit)
