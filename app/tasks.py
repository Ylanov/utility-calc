import os
import shutil
import zipfile
import logging
from datetime import datetime
from celery import group, chain
from app.worker import celery
from app.database import SessionLocalSync
# ИЗМЕНЕНИЕ: Добавлен импорт Adjustment
from app.models import MeterReading, Tariff, BillingPeriod, Adjustment
from app.services.pdf_generator import generate_receipt_pdf
from sqlalchemy.orm import selectinload

# Настраиваем логгер для задач Celery.
logger = logging.getLogger(__name__)

# Путь для сохранения файлов, доступный и веб-контейнеру и воркеру
SHARED_STORAGE_PATH = "/app/static/generated_files"
os.makedirs(SHARED_STORAGE_PATH, exist_ok=True)


def get_sync_db():
    """Вспомогательная функция для получения синхронной сессии БД в задаче"""
    db = SessionLocalSync()
    try:
        return db
    finally:
        db.close()


@celery.task(name="generate_receipt_task")
def generate_receipt_task(reading_id: int) -> dict:
    """
    Фоновая задача генерации ОДНОГО PDF квитанции.
    Может вызываться как отдельно (для скачивания одной квитанции),
    так и в составе группы (для массовой генерации).
    """
    logger.info(f"Generating single PDF for reading_id={reading_id}")
    db = get_sync_db()

    try:
        # 1. Получаем данные показаний
        reading = db.query(MeterReading).options(
            selectinload(MeterReading.user),
            selectinload(MeterReading.period)
        ).filter(MeterReading.id == reading_id).first()

        if not (reading and reading.user and reading.period):
            logger.error(f"Incomplete data for reading {reading_id}")
            return {"status": "error", "message": "Incomplete data"}

        tariff = db.query(Tariff).filter(Tariff.id == 1).first()
        if not tariff:
            return {"status": "error", "message": "Tariff not found"}

        prev_reading = db.query(MeterReading).filter(
            MeterReading.user_id == reading.user_id,
            MeterReading.is_approved == True,
            MeterReading.created_at < reading.created_at
        ).order_by(MeterReading.created_at.desc()).first()

        # ИЗМЕНЕНИЕ: Получаем корректировки (Adjustments) для этого пользователя и периода
        adjustments = db.query(Adjustment).filter(
            Adjustment.user_id == reading.user_id,
            Adjustment.period_id == reading.period_id
        ).all()

        # 2. Генерируем PDF
        final_path = generate_receipt_pdf(
            user=reading.user,
            reading=reading,
            period=reading.period,
            tariff=tariff,
            prev_reading=prev_reading,
            adjustments=adjustments,  # ИЗМЕНЕНИЕ: Передаем список корректировок
            output_dir=SHARED_STORAGE_PATH
        )

        # Права доступа
        os.chmod(final_path, 0o644)
        filename = os.path.basename(final_path)

        logger.info(f"PDF generated: {filename}")

        # Возвращаем путь, чтобы следующая задача могла его использовать
        return {"status": "ok", "path": final_path, "filename": filename}

    except Exception as e:
        logger.exception(f"Error generating PDF for reading {reading_id}")
        return {"status": "error", "message": str(e)}

    finally:
        db.close()


@celery.task(name="create_zip_archive_task")
def create_zip_archive_task(results: list) -> dict:
    """
    Финальная задача: собирает результаты от группы генераторов PDF и создает ZIP.
    Принимает список результатов от generate_receipt_task.
    """
    # Фильтруем успешные результаты
    successful_files = [res["path"] for res in results if res and res.get("status") == "ok"]

    if not successful_files:
        logger.error("No successful PDFs were generated. Aborting ZIP creation.")
        return {"status": "error", "message": "Не удалось создать ни одного файла."}

    logger.info(f"Starting ZIP creation with {len(successful_files)} files.")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_filename = f"Receipts_Bulk_{timestamp}.zip"
    zip_path = os.path.join(SHARED_STORAGE_PATH, zip_filename)

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in successful_files:
                if os.path.exists(file_path):
                    # Добавляем файл в корень архива
                    zipf.write(file_path, os.path.basename(file_path))

        # Права доступа на архив
        os.chmod(zip_path, 0o644)

        # Очистка: удаляем одиночные PDF-файлы, так как они уже в архиве
        for file_path in successful_files:
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
            except OSError as e:
                logger.warning(f"Could not remove temporary PDF {file_path}: {e}")

        logger.info(f"ZIP archive created successfully: {zip_path}")

        return {
            "status": "done",
            "filename": zip_filename,
            "path": zip_path,
            "count": len(successful_files)
        }

    except Exception as e:
        logger.exception("Failed to create ZIP archive.")
        return {"status": "error", "message": f"Ошибка архивации: {e}"}


@celery.task(name="start_bulk_receipt_generation")
def start_bulk_receipt_generation(period_id: int):
    """
    Стартовая задача-оркестратор.
    1. Находит все ID квитанций за период.
    2. Создает цепочку (chain): группа параллельных задач -> задача архивации.
    3. Запускает цепочку.
    """
    logger.info(f"Starting orchestration for period_id={period_id}")
    db = get_sync_db()

    try:
        # Получаем только ID, это очень быстро
        readings_query = db.query(MeterReading.id).filter(
            MeterReading.period_id == period_id,
            MeterReading.is_approved == True
        ).all()

        reading_ids = [r.id for r in readings_query]

        if not reading_ids:
            logger.warning(f"No approved readings found for period {period_id}.")
            return {"status": "error", "message": "Нет утвержденных показаний за этот период"}

        logger.info(f"Found {len(reading_ids)} readings. Launching parallel tasks.")

        # CANVAS: Создаем структуру задач
        workflow = chain(
            group(generate_receipt_task.s(rid) for rid in reading_ids),
            create_zip_archive_task.s()
        )

        # Запускаем workflow асинхронно
        async_result = workflow.apply_async()

        return {"task_id": async_result.id, "status": "processing"}

    except Exception as e:
        logger.exception(f"Orchestration failed for period {period_id}")
        return {"status": "error", "message": str(e)}

    finally:
        db.close()