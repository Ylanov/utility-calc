# app/modules/utility/tasks.py
import os
import shutil
import zipfile
import logging
import tempfile
import asyncio
from datetime import datetime, timezone
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
from app.modules.utility.models import MeterReading, Tariff, BillingPeriod, Adjustment, SystemSetting, User
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
    """
    Генерация одной квитанции, загрузка в S3 и удаление локального файла.
    Используется администратором при ручном запросе конкретной квитанции.
    """
    logger.info(f"[PDF] Start generation reading_id={reading_id}")
    db = None
    try:
        db = get_sync_db()
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

        user_tariff_id = getattr(user, 'tariff_id', None)
        tariff = None
        if user_tariff_id:
            tariff = db.query(Tariff).filter(Tariff.id == user_tariff_id).first()
        if not tariff:
            tariff = db.query(Tariff).filter(Tariff.is_active).order_by(Tariff.id).first()
        if not tariff:
            raise ValueError("No active tariffs found in the system for receipt generation.")

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
        final_path = generate_receipt_pdf(
            user=user,
            room=room,
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
            url = s3_service.get_presigned_url(object_name, expiration=600)
            return {"status": "ok", "s3_key": object_name, "download_url": url, "filename": filename}
        else:
            # S3 недоступен — перекладываем файл в статику, отдаём прямую ссылку
            logger.warning(f"[PDF] S3 unavailable, falling back to static dir for reading_id={reading_id}")
            static_dir = "/app/static/generated_files"
            os.makedirs(static_dir, exist_ok=True)
            static_path = os.path.join(static_dir, filename)
            shutil.move(final_path, static_path)
            local_url = f"/generated_files/{filename}"
            return {"status": "ok", "s3_key": None, "download_url": local_url, "filename": filename}

    except Exception as error:
        logger.exception(f"[PDF] Generation failed for reading_id={reading_id}")
        if db:
            db.rollback()
        raise
    finally:
        if db:
            db.close()


@celery.task(name="start_bulk_receipt_generation", queue="heavy")
def start_bulk_receipt_generation(period_id: int):
    """
    Генерирует все PDF локально кусками (chunks), пакует в ZIP и
    отправляет в S3 одним файлом. Решает проблему DDoS базы, Redis и S3.
    """
    logger.info(f"[ZIP] Start bulk generation period={period_id}")
    db = None
    try:
        db = get_sync_db()
        period = db.query(BillingPeriod).filter(BillingPeriod.id == period_id).first()
        if not period:
            return {"status": "error", "message": "Период не найден"}

        reading_ids_tuples = db.query(MeterReading.id).filter(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True)
        ).all()
        reading_ids = [r[0] for r in reading_ids_tuples]

        if not reading_ids:
            return {"status": "error", "message": "Нет утвержденных показаний"}

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        zip_name = f"Receipts_{period.name.replace(' ', '_')}_{timestamp}.zip"
        zip_s3_key = f"archives/{zip_name}"

        default_tariff = db.query(Tariff).filter(Tariff.is_active).order_by(Tariff.id).first()

        failed_ids = []

        with tempfile.TemporaryDirectory() as tmpdirname:
            zip_local_path = os.path.join(tmpdirname, zip_name)

            with zipfile.ZipFile(zip_local_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                chunk_size = 200  # Обрабатываем по 200 квитанций за раз (защита от OOM)

                for i in range(0, len(reading_ids), chunk_size):
                    chunk_ids = reading_ids[i:i + chunk_size]

                    # Жадная загрузка (Eager Load) для куска, чтобы избежать N+1 запросов
                    readings = db.query(MeterReading).options(
                        selectinload(MeterReading.user).selectinload(User.room)
                    ).filter(MeterReading.id.in_(chunk_ids)).all()

                    for r in readings:
                        try:
                            # Получаем корректировки
                            adjustments = db.query(Adjustment).filter(
                                Adjustment.user_id == r.user_id,
                                Adjustment.period_id == period_id
                            ).all()

                            # Предыдущее показание
                            prev_reading = db.query(MeterReading).filter(
                                MeterReading.room_id == r.room_id,
                                MeterReading.is_approved.is_(True),
                                MeterReading.created_at < r.created_at
                            ).order_by(MeterReading.created_at.desc()).first()

                            tariff = db.query(Tariff).filter(Tariff.id == r.user.tariff_id).first() if r.user.tariff_id else default_tariff

                            # Генерируем PDF локально
                            pdf_path = generate_receipt_pdf(
                                user=r.user, room=r.user.room, reading=r, period=period,
                                tariff=tariff, prev_reading=prev_reading,
                                adjustments=adjustments, output_dir=tmpdirname
                            )

                            filename = os.path.basename(pdf_path)
                            zipf.write(pdf_path, arcname=filename)
                            os.remove(pdf_path)  # Сразу удаляем PDF, бережем диск

                        except Exception as e:
                            logger.error(f"Error generating PDF for reading {r.id}: {e}")
                            failed_ids.append(r.id)

            if failed_ids:
                logger.warning(f"[ZIP] {len(failed_ids)} PDF(s) failed: {failed_ids}")

            # Загружаем готовый архив в S3
            if s3_service.upload_file(zip_local_path, zip_s3_key):
                url = s3_service.get_presigned_url(zip_s3_key, expiration=86400)  # Ссылка живет 24 часа
                return {
                    "status": "done", "s3_key": zip_s3_key, "download_url": url,
                    "count": len(reading_ids), "failed_count": len(failed_ids), "failed_ids": failed_ids
                }
            else:
                # S3 недоступен — перекладываем ZIP в статику
                logger.warning("[ZIP] S3 unavailable, falling back to static dir for archive")
                static_dir = "/app/static/generated_files"
                os.makedirs(static_dir, exist_ok=True)
                static_zip_path = os.path.join(static_dir, zip_name)
                shutil.copy2(zip_local_path, static_zip_path)
                local_url = f"/generated_files/{zip_name}"
                return {
                    "status": "done", "s3_key": None, "download_url": local_url,
                    "count": len(reading_ids), "failed_count": len(failed_ids), "failed_ids": failed_ids
                }

    except Exception as error:
        logger.exception("[ZIP] Generation failed")
        if db:
            db.rollback()
        return {"status": "error", "message": str(error)}
    finally:
        if db:
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
    db = None
    try:
        db = get_sync_db()
        result = sync_import_debts_process(file_path, db, account_type)
        return result
    except Exception:
        logger.exception("[IMPORT] Failed")
        if db:
            db.rollback()
        raise
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception as error:
            logger.warning(f"[IMPORT] File cleanup failed: {error}")
        if db:
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
    db = None
    try:
        db = get_sync_db()
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
        if db:
            db.rollback()
    finally:
        if db:
            db.close()


@celery.task(name="activate_scheduled_tariffs_task", queue="default")
def activate_scheduled_tariffs_task():
    """
    Ежедневная задача (Beat): автоматическая активация тарифов по дате вступления в силу.
    Находит тарифы с effective_from <= сейчас и is_active=False → устанавливает is_active=True.
    """
    logger.info("[TARIFF] Checking scheduled tariffs for activation...")
    db = None
    try:
        db = get_sync_db()
        from app.modules.utility.models import Tariff
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        tariffs_to_activate = db.query(Tariff).filter(
            Tariff.effective_from.is_not(None),
            Tariff.effective_from <= now,
            Tariff.is_active.is_(False)
        ).all()

        if not tariffs_to_activate:
            logger.info("[TARIFF] No scheduled tariffs to activate.")
            return {"activated": 0}

        for t in tariffs_to_activate:
            t.is_active = True
            logger.info(f"[TARIFF] Activated tariff '{t.name}' (id={t.id}), effective_from={t.effective_from}")

        db.commit()
        logger.info(f"[TARIFF] Activated {len(tariffs_to_activate)} tariff(s).")
        return {"activated": len(tariffs_to_activate), "ids": [t.id for t in tariffs_to_activate]}

    except Exception:
        logger.exception("[TARIFF] activate_scheduled_tariffs_task failed")
        if db:
            db.rollback()
        return {"activated": 0, "error": "task failed"}
    finally:
        if db:
            db.close()


@celery.task(name="detect_anomalies_task", queue="default")
def detect_anomalies_task(reading_id: int):
    """
    Анализ аномалий для одного показания.
    Безопасное управление DB-сессиями.
    """
    db = None
    try:
        db = get_sync_db()
        reading = db.query(MeterReading).options(
            selectinload(MeterReading.user).selectinload(User.room)
        ).filter(MeterReading.id == reading_id).first()

        if not reading or reading.is_approved or not reading.room_id:
            return

        history = db.query(MeterReading).filter(
            MeterReading.room_id == reading.room_id,
            MeterReading.is_approved.is_(True)
        ).order_by(MeterReading.created_at.desc()).limit(6).all()

        from app.modules.utility.services.anomaly_detector import check_reading_for_anomalies_v2
        flags, score = check_reading_for_anomalies_v2(reading, history, user=reading.user)

        reading.anomaly_flags = flags if flags else None
        reading.anomaly_score = score
        db.commit()
    except Exception as e:
        logger.exception(f"Anomaly detection failed for reading_id={reading_id}: {e}")
        if db:
            db.rollback()
    finally:
        if db:
            db.close()