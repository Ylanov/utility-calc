import os
import zipfile
import logging
import tempfile
import asyncio
from datetime import datetime
from celery import group, chain
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from redis import asyncio as aioredis
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from redis import Redis

from app.worker import celery
from app.core.database import SessionLocalSync
from app.core.config import settings
# ИЗМЕНЕНИЕ: Добавляем импорт Room
from app.modules.utility.models import MeterReading, Tariff, BillingPeriod, Adjustment, SystemSetting, User, Room
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.debt_import import sync_import_debts_process
from app.modules.utility.services.s3_client import s3_service
from app.modules.utility.services.billing import close_current_period

logger = logging.getLogger(__name__)


def get_sync_db():
    return SessionLocalSync()


@celery.task(
    name="generate_receipt_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 3, "countdown": 10},
    retry_backoff=True
)
def generate_receipt_task(reading_id: int) -> dict:
    """Генерация одной квитанции, загрузка в S3 и удаление локального файла."""
    logger.info(f"[PDF] Start generation reading_id={reading_id}")
    db = get_sync_db()
    try:
        # ИЗМЕНЕНИЕ: Загружаем одним запросом показание, жильца, его комнату и период
        reading = (
            db.query(MeterReading)
            .options(
                selectinload(MeterReading.user).selectinload(User.room),
                selectinload(MeterReading.period)
            )
            .filter(MeterReading.id == reading_id)
            .first()
        )
        if not reading or not reading.user or not reading.period or not reading.user.room:
            raise ValueError("Incomplete data: reading, user, room, or period is missing.")

        user = reading.user
        room = user.room
        period = reading.period

        # --- Логика тарифов остается без изменений ---
        user_tariff_id = getattr(user, 'tariff_id', None)
        tariff = None
        if user_tariff_id:
            tariff = db.query(Tariff).filter(Tariff.id == user_tariff_id).first()
        if not tariff:
            tariff = db.query(Tariff).filter(Tariff.is_active).order_by(Tariff.id).first()
        if not tariff:
            raise ValueError("No active tariffs found in the system for receipt generation.")

        # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Ищем предыдущее показание по room_id ---
        prev_reading = (
            db.query(MeterReading)
            .filter(
                MeterReading.room_id == room.id,
                MeterReading.is_approved.is_(True),
                MeterReading.created_at < reading.created_at
            )
            .order_by(MeterReading.created_at.desc())
            .first()
        )

        adjustments = (
            db.query(Adjustment)
            .filter(
                Adjustment.user_id == user.id,
                Adjustment.period_id == period.id
            )
            .all()
        )

        temp_dir = "/tmp"
        # ИЗМЕНЕНИЕ: Передаем объект room в генератор PDF
        final_path = generate_receipt_pdf(
            user=user,
            room=room,  # <--- Передаем комнату
            reading=reading,
            period=period,
            tariff=tariff,
            prev_reading=prev_reading,
            adjustments=adjustments,
            output_dir=temp_dir
        )

        filename = os.path.basename(final_path)
        object_name = f"receipts/{period.id}/{filename}"

        if s3_service.upload_file(final_path, object_name):
            os.remove(final_path)
            logger.info(f"[PDF] Uploaded to S3: {object_name}")
            return {"status": "ok", "s3_key": object_name, "filename": filename}
        else:
            raise RuntimeError("S3 Upload Failed")

    except Exception:
        logger.exception("[PDF] Generation failed")
        raise
    finally:
        db.close()


@celery.task(name="create_zip_archive_task")
def create_zip_archive_task(results) -> dict:
    """Сборка ZIP-архива из файлов в S3."""
    if isinstance(results, dict):
        results = [results]

    s3_keys = [r["s3_key"] for r in results if isinstance(r, dict) and r.get("status") == "ok" and "s3_key" in r]

    if not s3_keys:
        logger.error("[ZIP] No valid files to archive")
        return {"status": "error", "message": "Нет файлов для архивации"}

    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    zip_name = f"Receipts_{timestamp}.zip"
    zip_s3_key = f"archives/{zip_name}"

    try:
        with tempfile.TemporaryDirectory() as tmpdirname:
            logger.info(f"[ZIP] Using temp dir: {tmpdirname}")
            zip_local_path = os.path.join(tmpdirname, zip_name)

            with zipfile.ZipFile(zip_local_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for key in s3_keys:
                    local_filename = os.path.basename(key)
                    local_file_path = os.path.join(tmpdirname, local_filename)
                    s3_service.s3.download_file(s3_service.bucket, key, local_file_path)
                    zipf.write(local_file_path, arcname=local_filename)

            if s3_service.upload_file(zip_local_path, zip_s3_key):
                logger.info(f"[ZIP] Archive uploaded to S3: {zip_s3_key}")
                return {"status": "done", "filename": zip_name, "s3_key": zip_s3_key, "count": len(s3_keys)}
            else:
                raise RuntimeError("Failed to upload ZIP archive to S3")

    except Exception as error:
        logger.exception("[ZIP] Creation failed")
        return {"status": "error", "message": str(error)}


@celery.task(name="start_bulk_receipt_generation")
def start_bulk_receipt_generation(period_id: int):
    """Запускает цепочку: Генерация всех PDF -> Сборка их в один ZIP."""
    logger.info(f"[FLOW] Start bulk generation period={period_id}")
    db = get_sync_db()
    try:
        period = db.query(BillingPeriod).filter(BillingPeriod.id == period_id).first()
        if not period:
            return {"status": "error", "message": "Период не найден"}

        reading_ids = [
            r.id for r in db.query(MeterReading.id)
            .filter(MeterReading.period_id == period_id, MeterReading.is_approved.is_(True))
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

        return {"status": "processing", "task_id": result.id, "count": len(reading_ids)}

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
def import_debts_task(file_path: str, account_type: str) -> dict:
    """Фоновая задача импорта долгов."""
    logger.info(f"[IMPORT] Start {file_path} for Account {account_type}")
    db = get_sync_db()
    try:
        result = sync_import_debts_process(file_path, db, account_type)
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


async def run_async_close_period(admin_user_id: int):
    """Асинхронная обертка для логики закрытия периода."""
    try:
        redis = aioredis.from_url(settings.REDIS_URL, encoding="utf8", decode_responses=True)
        FastAPICache.init(RedisBackend(redis), prefix="fastapi-cache")
    except Exception as e:
        logger.warning(f"Could not init cache in worker: {e}")

    async_engine = create_async_engine(
        settings.DATABASE_URL_ASYNC,
        echo=False,
        future=True,
        pool_pre_ping=True,
        connect_args={"prepared_statement_cache_size": 0, "statement_cache_size": 0, "command_timeout": 60}
    )

    local_session_maker = sessionmaker(
        bind=async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    try:
        async with local_session_maker() as db:
            try:
                result = await close_current_period(db=db, admin_user_id=admin_user_id)
                await db.commit()
                await FastAPICache.clear(namespace="periods")
                return result
            except Exception as e:
                await db.rollback()
                raise e
    finally:
        await async_engine.dispose()


@celery.task(name="close_period_task", queue="heavy")
def close_period_task(admin_user_id: int):
    """Celery-задача с Redis Lock (защита от двойного запуска)"""
    logger.info(f"Starting close_period_task for admin {admin_user_id}")
    redis_client = Redis.from_url(settings.REDIS_URL)
    lock_key = "lock:close_period"
    try:
        result = asyncio.run(run_async_close_period(admin_user_id))
        return result
    except Exception as e:
        logger.exception("Error in close_period_task")
        return {"status": "error", "message": str(e)}
    finally:
        try:
            redis_client.delete(lock_key)
            logger.info("[LOCK] Released close_period lock")
        except Exception as e:
            logger.warning(f"[LOCK] Failed to release: {e}")


@celery.task(name="check_auto_period_task")
def check_auto_period_task():
    """Ежедневная задача (Beat): автоматическое управление периодами."""
    logger.info("[AUTO] Checking period automation...")
    db = get_sync_db()
    try:
        start_setting = db.query(SystemSetting).filter_by(key="submission_start_day").first()
        end_setting = db.query(SystemSetting).filter_by(key="submission_end_day").first()
        start_day = int(start_setting.value) if start_setting else 20
        end_day = int(end_setting.value) if end_setting else 25
        today = datetime.now()
        current_day = today.day
        active = db.query(BillingPeriod).filter_by(is_active=True).first()

        if start_day <= current_day <= end_day:
            if not active:
                month_names = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь",
                               "Октябрь", "Ноябрь", "Декабрь"]
                period_name = f"{month_names[today.month]} {today.year}"
                exists = db.query(BillingPeriod).filter_by(name=period_name).first()
                if not exists:
                    new_period = BillingPeriod(name=period_name, is_active=True)
                    db.add(new_period)
                    db.commit()
                    logger.info(f"[AUTO] Opened new period: {period_name}")
        elif active:
            is_after_end = current_day > end_day
            is_new_month = current_day < start_day
            if is_after_end or is_new_month:
                redis_client = Redis.from_url(settings.REDIS_URL)
                lock_key = "lock:close_period"
                lock_acquired = redis_client.set(lock_key, "1", nx=True, ex=1800)
                if lock_acquired:
                    admin = db.query(User).filter_by(username="admin").first()
                    if admin:
                        close_period_task.delay(admin.id)
                        logger.info(f"[AUTO] Triggered closing task for period '{active.name}'")
                else:
                    logger.info("[AUTO] Close already running, skip duplicate")
    except Exception:
        logger.exception("[AUTO] Automation failed")
    finally:
        db.close()


@celery.task(name="detect_anomalies_task", queue="default")
def detect_anomalies_task(reading_id: int):
    """Анализ аномалий для одного показания."""
    db = get_sync_db()
    try:
        # ИЗМЕНЕНИЕ: Загружаем показание вместе с user и room
        reading = db.query(MeterReading).options(
            selectinload(MeterReading.user).selectinload(User.room)
        ).filter(MeterReading.id == reading_id).first()

        if not reading or reading.is_approved or not reading.user or not reading.user.room:
            return

        # --- КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: Ищем историю по room_id ---
        history = db.query(MeterReading).filter(
            MeterReading.room_id == reading.user.room_id,
            MeterReading.is_approved == True
        ).order_by(MeterReading.created_at.desc()).limit(6).all()

        from app.modules.utility.services.anomaly_detector import check_reading_for_anomalies_v2
        flags, score = check_reading_for_anomalies_v2(reading, history, user=reading.user)

        reading.anomaly_flags = flags if flags else None
        reading.anomaly_score = score
        db.commit()
    except Exception as e:
        logger.exception(f"Anomaly detection failed for reading_id={reading_id}: {e}")
        db.rollback()
    finally:
        db.close()