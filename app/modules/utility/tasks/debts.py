# Долги 1С: фоновый импорт xlsx (черновик) и авто-выгрузка жильцам после
# ежедневного сбора. Вербатим-перенос из tasks.py (строки 305-408), 1:1.

import os
import asyncio

from app.worker import celery
from app.core.config import settings
from app.modules.utility.services.debt_import import sync_import_debts_process

from ._shared import logger, sync_db_session


@celery.task(
    name="import_debts_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 2, "countdown": 15},
    retry_backoff=True
)
def import_debts_task(
    file_path: str,
    account_type: str,
    started_by_id: int | None = None,
    started_by_username: str | None = None,
    batch_id: str | None = None,
    original_file_name: str | None = None,
    period_id: int | None = None,
) -> dict:
    """Фоновая задача импорта долгов.

    batch_id — общий UUID для парной загрузки 205+209. Оба DebtImportLog
    получают один batch_id, UI группирует их как одну операцию.
    original_file_name — оригинальное имя файла из upload (без UUID),
    чтобы в истории показывать «209-апрель-2026.xlsx», а не uuid'ы.

    Файл с ARCHIVE_PATH БОЛЬШЕ НЕ УДАЛЯЕТСЯ — он архивируется и
    привязывается к DebtImportLog.archive_path. Очистка делает retention-
    task раз в неделю (см. analyzer_settings debt.archive_retention_days).

    Файл из legacy TEMP_DIR (если кто-то ещё его использует) удаляется
    как раньше — у него нет архивного смысла.
    """
    logger.info(
        f"[IMPORT] Start {file_path} for Account {account_type} "
        f"by user_id={started_by_id} ({started_by_username}) batch={batch_id}"
    )
    with sync_db_session() as db:
        result = sync_import_debts_process(
            file_path, db, account_type,
            started_by_id=started_by_id,
            started_by_username=started_by_username,
            batch_id=batch_id,
            original_file_name=original_file_name,
            period_id=period_id,
            # Гейт «Выгрузить»: импорт 1С грузит в ЧЕРНОВИК, в показания жильцов
            # не пишет. Долги применяет отдельная кнопка (/debts/publish).
            stage_only=True,
        )

    # Архивные файлы НЕ удаляем — они привязаны к DebtImportLog.archive_path
    # и используются для скачивания / диагностики. Удалит retention-task.
    # Файлы из legacy temp_imports — удаляем как раньше.
    if "/temp_imports/" in file_path:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as error:
            logger.warning(f"[IMPORT] Legacy file cleanup failed: {error}")
    return result


@celery.task(
    name="onec_autopublish_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 2, "countdown": 30},
)
def onec_autopublish_task(batch_id: str | None = None) -> dict:
    """Авто-выгрузка долгов 1С жильцам после ежедневного сбора.

    Запускается ПОСЛЕДНИМ звеном цепочки onec_sync (после staged-импортов
    209/205): берёт свежие черновики и пишет долги/переплаты в показания
    активного периода — кошелёк в ЛК по QR, квитанция и админка всегда
    показывают свежее из 1С, без ручной кнопки «Выгрузить».

    С предохранителем (guard=True): аномальный сбор, который обнулил бы массу
    ненулевых долгов (как баг парсинга), НЕ выгружается — черновик остаётся на
    ручную проверку в «Долги 1С». Итог пишется в onec-статус (last_autopublish).

    Выгрузка коммитится атомарно в конце publish_onec_debts → краш до коммита
    откатывается, и autoretry безопасен (повтор берёт тот же staged-черновик).
    """
    async def _run():
        # Свой async-engine на вызов (паттерн scan_resident_problems_task):
        # asyncio.run создаёт новый event loop, asyncpg-коннекты привязаны к нему.
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession as _AS
        from sqlalchemy.orm import sessionmaker as _smaker
        from app.modules.utility.services.onec_publish import (
            publish_onec_debts, record_autopublish_status,
        )
        _engine = create_async_engine(
            settings.DATABASE_URL_ASYNC,
            echo=False, future=True, pool_pre_ping=True,
            connect_args={"prepared_statement_cache_size": 0,
                          "statement_cache_size": 0, "command_timeout": 120},
        )
        _mk = _smaker(bind=_engine, class_=_AS, expire_on_commit=False, autoflush=False)
        try:
            async with _mk() as db:
                result = await publish_onec_debts(db, guard=True)
                await record_autopublish_status(db, result)
                return result
        finally:
            await _engine.dispose()

    result = asyncio.run(_run())
    logger.info("[onec_autopublish_task] batch=%s %s", batch_id, result)
    return result
