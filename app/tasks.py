import os
import zipfile
import logging
from datetime import datetime

from celery import group, chain
from sqlalchemy.orm import selectinload

from app.worker import celery
from app.database import SessionLocalSync

from app.models import MeterReading, Tariff, BillingPeriod, Adjustment
from app.services.pdf_generator import generate_receipt_pdf
from app.services.debt_import import sync_import_debts_process


logger = logging.getLogger(__name__)

SHARED_STORAGE_PATH = "/app/static/generated_files"
os.makedirs(SHARED_STORAGE_PATH, exist_ok=True)


def get_sync_db():
    return SessionLocalSync()


@celery.task(
    name="generate_receipt_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 10},
    retry_backoff=True
)
def generate_receipt_task(reading_id: int) -> dict:
    logger.info(f"[PDF] Start generation reading_id={reading_id}")
    db = get_sync_db()

    try:
        reading = (
            db.query(MeterReading)
            .options(
                selectinload(MeterReading.user),
                selectinload(MeterReading.period)
            )
            .filter(MeterReading.id == reading_id)
            .first()
        )

        if not reading or not reading.user or not reading.period:
            raise ValueError("Incomplete reading data")

        period = reading.period

        # Берем активный тариф
        tariff = db.query(Tariff).filter(Tariff.is_active == True).first()
        if not tariff:
            raise ValueError("Active tariff not found")

        prev_reading = (
            db.query(MeterReading)
            .filter(
                MeterReading.user_id == reading.user_id,
                MeterReading.is_approved.is_(True),
                MeterReading.created_at < reading.created_at
            )
            .order_by(MeterReading.created_at.desc())
            .first()
        )

        adjustments = (
            db.query(Adjustment)
            .filter(
                Adjustment.user_id == reading.user_id,
                Adjustment.period_id == reading.period_id
            )
            .all()
        )

        # --- ИСПРАВЛЕНИЕ: Передаем prev_reading ---
        final_path = generate_receipt_pdf(
            user=reading.user,
            reading=reading,
            period=period,
            tariff=tariff,
            prev_reading=prev_reading,  # <--- ВОТ ЭТО ВАЖНО
            adjustments=adjustments,
            output_dir=SHARED_STORAGE_PATH
        )

        os.chmod(final_path, 0o644)
        filename = os.path.basename(final_path)

        logger.info(f"[PDF] Generated {filename}")

        return {
            "status": "ok",
            "path": final_path,
            "filename": filename
        }

    except Exception:
        logger.exception("[PDF] Generation failed")
        raise

    finally:
        db.close()


@celery.task(name="create_zip_archive_task")
def create_zip_archive_task(results) -> dict:
    if isinstance(results, dict):
        results = [results]

    successful_files = [
        r["path"]
        for r in results
        if isinstance(r, dict) and r.get("status") == "ok"
    ]

    if not successful_files:
        logger.error("[ZIP] No valid files")
        return {
            "status": "error",
            "message": "Нет файлов для архивации"
        }

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    zip_name = f"Receipts_{timestamp}.zip"
    zip_path = os.path.join(SHARED_STORAGE_PATH, zip_name)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path in successful_files:
                if os.path.exists(file_path):
                    zipf.write(file_path, arcname=os.path.basename(file_path))

        os.chmod(zip_path, 0o644)

        for file_path in successful_files:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as error:
                    logger.warning(f"[ZIP] Cleanup failed: {error}")

        logger.info(f"[ZIP] Archive created {zip_name}")

        return {
            "status": "done",
            "filename": zip_name,
            "path": zip_path,
            "count": len(successful_files)
        }

    except Exception as error:
        logger.exception("[ZIP] Creation failed")
        return {
            "status": "error",
            "message": str(error)
        }


@celery.task(name="start_bulk_receipt_generation")
def start_bulk_receipt_generation(period_id: int):
    logger.info(f"[FLOW] Start bulk generation period={period_id}")
    db = get_sync_db()

    try:
        period = db.query(BillingPeriod).filter(BillingPeriod.id == period_id).first()
        if not period:
            return {"status": "error", "message": "Период не найден"}

        reading_ids = [
            r.id
            for r in db.query(MeterReading.id)
            .filter(
                MeterReading.period_id == period_id,
                MeterReading.is_approved.is_(True)
            )
            .all()
        ]

        if not reading_ids:
            return {"status": "error", "message": "Нет утвержденных показаний"}

        workflow = chain(
            group(generate_receipt_task.s(rid) for rid in reading_ids),
            create_zip_archive_task.s()
        )

        result = workflow.apply_async()

        logger.info(f"[FLOW] Started task_id={result.id}")

        return {
            "status": "processing",
            "task_id": result.id,
            "count": len(reading_ids)
        }

    except Exception as error:
        logger.exception("[FLOW] Failed")
        return {"status": "error", "message": str(error)}

    finally:
        db.close()


@celery.task(
    name="import_debts_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 2, "countdown": 15},
    retry_backoff=True
)
def import_debts_task(file_path: str) -> dict:
    logger.info(f"[IMPORT] Start {file_path}")
    db = get_sync_db()

    try:
        result = sync_import_debts_process(file_path, db)
        return result

    except Exception:
        logger.exception("[IMPORT] Failed")
        raise

    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as error:
            logger.warning(f"[IMPORT] File cleanup failed: {error}")

        db.close()