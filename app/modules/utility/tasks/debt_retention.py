# Ретеншн архивных xlsx импортов 1С (DebtImportLog.archive_path).
# Вербатим-перенос из tasks.py (строки 496-558), поведение 1:1.

import os

from app.worker import celery

from ._shared import logger, sync_db_session


@celery.task(name="cleanup_debt_archives_task")
def cleanup_debt_archives_task() -> dict:
    """Очистка архивных xlsx из 1С (DebtImportLog.archive_path).

    Каждое воскресенье в 03:15 (см. worker.py beat_schedule). Удаляет файлы
    старше retention. retention берётся из:
      - DebtImportLog.retention_days (per-log override, если задан)
      - иначе analyzer_settings.debt.archive_retention_days (default 730)

    Сами DebtImportLog НЕ удаляются — только физический файл, archive_path
    обнуляется (чтобы UI «Скачать» давал понятный 404 вместо битого пути).

    Что НЕ делает:
      - не трогает логи без archive_path (старые до миграции debts_002)
      - не трогает file_name / not_found_users / snapshot_data —
        для истории и undo они остаются доступны
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select
    from app.modules.utility.models import DebtImportLog
    from app.modules.utility.services.analyzer_config import config

    default_retention = config.get_int("debt.archive_retention_days", 730)

    deleted_count = 0
    skipped_missing = 0  # файл уже отсутствует, просто чистим ссылку

    with sync_db_session() as db:
        logs = db.execute(
            select(DebtImportLog).where(DebtImportLog.archive_path.isnot(None))
        ).scalars().all()

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for log in logs:
            retention = log.retention_days or default_retention
            if not log.started_at:
                continue
            cutoff = now - timedelta(days=retention)
            if log.started_at >= cutoff:
                continue

            path = log.archive_path
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                    deleted_count += 1
                except OSError as e:
                    logger.warning(f"[DEBT-RETENTION] Failed to delete {path}: {e}")
                    continue
            else:
                skipped_missing += 1

            # Обнуляем archive_path всегда — даже если файл отсутствовал
            # (значит был удалён руками или предыдущим прогоном таска).
            log.archive_path = None

        db.commit()

    logger.info(
        f"[DEBT-RETENTION] Deleted {deleted_count} archives, "
        f"cleaned {skipped_missing} dangling references."
    )
    return {"deleted": deleted_count, "skipped_missing": skipped_missing}
