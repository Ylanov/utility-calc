# app/modules/utility/tasks.py
import os
import shutil
import zipfile
import logging
import tempfile
import asyncio
from contextlib import contextmanager
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
    # Сохранён для обратной совместимости. В новом коде используй sync_db_session().
    return SessionLocalSync()


@contextmanager
def sync_db_session():
    """
    Контекст-менеджер для sync-сессии внутри Celery-задач.

    Гарантирует:
    - rollback при любом исключении (SQLAlchemy session leak не возникнет);
    - close в finally (возврат соединения в пул/закрытие).

    Раньше в задачах был паттерн `db = None; try: db = get_sync_db(); ...`
    — при redeploy или неожиданном исключении в get_sync_db() сессия не
    закрывалась, и через 2-3 часа пиковой нагрузки pool исчерпывался.
    """
    session = SessionLocalSync()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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
    try:
        with sync_db_session() as db:
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

            # Тариф через единый сервис: Room.tariff_id → User.tariff_id → default.
            # Раньше код смотрел только User.tariff_id и не учитывал назначение
            # тарифа на комнату/общежитие → у жильцов в общежитии с особым тарифом
            # квитанция приходила по дефолтному.
            from app.modules.utility.services.tariff_cache import tariff_cache
            tariff = tariff_cache.get_effective_tariff(user=user, room=room)
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

        # S3-upload делаем ВНЕ транзакции — чтобы не держать соединение
        # с БД во время сетевого I/O.
        filename = os.path.basename(final_path)
        object_name = f"receipts/{period.id}/{filename}"

        if s3_service.upload_file(final_path, object_name):
            os.remove(final_path)
            logger.info(f"[PDF] Uploaded to S3: {object_name}")
            url = s3_service.get_presigned_url(object_name, expiration=600)
            return {"status": "ok", "s3_key": object_name, "download_url": url, "filename": filename}
        else:
            logger.warning(f"[PDF] S3 unavailable, falling back to static dir for reading_id={reading_id}")
            static_dir = "/app/static/generated_files"
            os.makedirs(static_dir, exist_ok=True)
            static_path = os.path.join(static_dir, filename)
            shutil.move(final_path, static_path)
            local_url = f"/generated_files/{filename}"
            return {"status": "ok", "s3_key": None, "download_url": local_url, "filename": filename}

    except Exception:
        logger.exception(f"[PDF] Generation failed for reading_id={reading_id}")
        raise


@celery.task(name="start_bulk_receipt_generation", queue="heavy")
def start_bulk_receipt_generation(period_id: int):
    """
    Генерирует все PDF локально кусками (chunks), пакует в ZIP и
    отправляет в S3 одним файлом. Решает проблему DDoS базы, Redis и S3.
    """
    logger.info(f"[ZIP] Start bulk generation period={period_id}")
    try:
      with sync_db_session() as db:
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

                            # Через единый кеш: Room.tariff_id → User.tariff_id → default
                            from app.modules.utility.services.tariff_cache import tariff_cache
                            tariff = tariff_cache.get_effective_tariff(user=r.user, room=r.user.room) or default_tariff

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
        return {"status": "error", "message": str(error)}


@celery.task(
    name="import_debts_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 2, "countdown": 15},
    retry_backoff=True
)
def import_debts_task(file_path: str, account_type: str) -> dict:
    """Фоновая задача импорта долгов.

    Транзакционность: sync_import_debts_process делает ОДИН commit в конце
    и полный rollback при любом исключении. Тасак ретраится до 2 раз
    (см. retry_kwargs) — временные сбои БД/Redis не приведут к частичному
    импорту.

    Файл удаляется ТОЛЬКО при успешном завершении. Если задача падает и
    будет ретрайнутся — файл остаётся на диске, чтобы ретрай мог его снова
    прочитать. Если все ретраи исчерпаны — файл остаётся (админ увидит
    ошибку и загрузит заново).
    """
    logger.info(f"[IMPORT] Start {file_path} for Account {account_type}")
    with sync_db_session() as db:
        result = sync_import_debts_process(file_path, db, account_type)

    # Сюда доходим только при успехе (ошибка пробрасывается из with).
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as error:
        logger.warning(f"[IMPORT] File cleanup failed: {error}")
    return result


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


@celery.task(
    name="close_period_task",
    queue="heavy",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 2, "countdown": 60},
)
def close_period_task(admin_user_id: int):
    """
    Celery-задача закрытия расчётного периода с защитой от двойного запуска.

    ИСПРАВЛЕНИЯ:
    1. Раньше Redis lock НЕ ставился — был только delete() в finally. Это значит
       что два админа одновременно могли запустить close → двойная авто-генерация
       показаний → двойные суммы. Теперь используем SET NX EX (атомарно).
    2. Раньше при ошибке возвращали {"status": "error"} — Celery считал задачу
       успешной, retry не работал. Теперь raise — autoretry_for сделает 2 попытки.
    3. Lock освобождаем по тому же значению (атомарно через Lua-скрипт), чтобы
       ретрай не убил чужой lock.

    Сама `close_current_period` уже атомарна: вся работа в одной транзакции,
    при exception — rollback. Период «полузакрытым» не останется.
    """
    logger.info(f"Starting close_period_task for admin {admin_user_id}")
    redis_client = Redis.from_url(settings.REDIS_URL)
    lock_key = "lock:close_period"
    lock_value = f"task-{admin_user_id}-{datetime.now(timezone.utc).timestamp()}"
    lock_ttl = 1800  # 30 минут — заведомо больше любого реального close_period

    # Атомарный SET NX EX — если ключа нет, ставим и возвращаем True.
    acquired = redis_client.set(lock_key, lock_value, nx=True, ex=lock_ttl)
    if not acquired:
        logger.warning("[LOCK] close_period уже выполняется другой задачей — пропускаем")
        return {"status": "skipped", "reason": "already_running"}

    try:
        result = asyncio.run(run_async_close_period(admin_user_id))
        logger.info(f"[CLOSE_PERIOD] Success: {result}")
        return result
    except Exception:
        logger.exception("[CLOSE_PERIOD] Failed — будет retry автоматически")
        raise  # пусть Celery увидит ошибку и сделает retry
    finally:
        # Освобождаем lock только если он наш (Lua-скрипт для атомарности).
        # Иначе после retry мы могли бы стереть чужой lock.
        try:
            release_script = """
            if redis.call('GET', KEYS[1]) == ARGV[1] then
                return redis.call('DEL', KEYS[1])
            else
                return 0
            end
            """
            redis_client.eval(release_script, 1, lock_key, lock_value)
            logger.info("[LOCK] Released close_period lock")
        except Exception as e:
            logger.warning(f"[LOCK] Failed to release: {e}")


@celery.task(name="check_auto_period_task")
def check_auto_period_task():
    """Ежедневная задача (Beat): автоматическое управление периодами."""
    logger.info("[AUTO] Checking period automation...")
    try:
        with sync_db_session() as db:
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


@celery.task(name="activate_scheduled_tariffs_task", queue="default")
def activate_scheduled_tariffs_task():
    """
    Ежедневная задача (Beat): автоматическая активация тарифов по дате вступления в силу.
    Находит тарифы с effective_from <= сейчас и is_active=False → устанавливает is_active=True.
    """
    logger.info("[TARIFF] Checking scheduled tariffs for activation...")
    try:
        with sync_db_session() as db:
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

            activated_ids = []
            for t in tariffs_to_activate:
                t.is_active = True
                activated_ids.append(t.id)
                logger.info(f"[TARIFF] Activated tariff '{t.name}' (id={t.id}), effective_from={t.effective_from}")

            db.commit()

            # ВАЖНО: без invalidate кеш tariff_cache на 10 мин продолжит
            # отдавать старые (is_active=False) тарифы как «несуществующие»,
            # и приоритет «Room.tariff_id» провалится на default. После этого
            # коммита нужно явно сбросить кеш, чтобы новые тарифы сразу
            # применялись в расчётах.
            try:
                from app.modules.utility.services.tariff_cache import tariff_cache
                tariff_cache.invalidate()
            except Exception:
                logger.warning("[TARIFF] Could not invalidate tariff_cache after activation")

            logger.info(f"[TARIFF] Activated {len(activated_ids)} tariff(s).")
            return {"activated": len(activated_ids), "ids": activated_ids}

    except Exception:
        logger.exception("[TARIFF] activate_scheduled_tariffs_task failed")
        return {"activated": 0, "error": "task failed"}


@celery.task(name="detect_anomalies_task", queue="default")
def detect_anomalies_task(reading_id: int):
    """
    Анализ аномалий для одного показания.
    Безопасное управление DB-сессиями.
    """
    try:
        with sync_db_session() as db:
            reading = db.query(MeterReading).options(
                selectinload(MeterReading.user).selectinload(User.room)
            ).filter(MeterReading.id == reading_id).first()

            if not reading or reading.is_approved or not reading.room_id:
                return

            # Пропускаем анализ для холостяков (per_capita) — они не подают
            # показания счётчиков, алгоритмы SPIKE/FLAT/TREND бесполезны и
            # создают шум в «Центре анализа». Для них релевантны только
            # финансовые флаги (DEBT_GROWING и др., считаются в другом месте).
            if reading.user and getattr(reading.user, "billing_mode", "by_meter") == "per_capita":
                logger.debug(f"Skipping anomaly detection for per_capita user (reading={reading.id})")
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