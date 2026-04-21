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


@celery.task(name="run_arsenal_analyzer_task", queue="default")
def run_arsenal_analyzer_task():
    """Периодическая проверка данных арсенала на нарушения / фрод-паттерны.
    Запускается Celery Beat'ом (раз в час — настраивается в worker.py).

    В Центре анализа появляются / обновляются записи ArsenalAnomalyFlag;
    устранённые ситуации автоматически resolveятся."""
    logger.info("[ARSENAL] Running analyzer...")
    try:
        from app.core.database import ArsenalSessionLocalSync
        from app.modules.arsenal.services.arsenal_analyzer import run_arsenal_analyzer

        with ArsenalSessionLocalSync() as db:
            results = run_arsenal_analyzer(db)
            db.commit()
        logger.info(f"[ARSENAL] Analyzer results: {results}")
        return results
    except Exception:
        logger.exception("[ARSENAL] analyzer task failed")
        return {"error": "task failed"}


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


# ==========================================================================
# ПОЛНЫЙ ПЕРЕРАСЧЁТ ПЕРИОДА
# ==========================================================================
# Контекст: показания утверждаются и сохраняют total_* значения, посчитанные
# с тарифом на момент утверждения. Если админ потом поменял тариф (или раньше
# его вообще не было), данные устарели — и 1С шлёт некорректные квитанции.
#
# Эта пара задач (_preview и _apply) пересчитывает ВСЕ approved MeterReading
# за данный period_id с текущим эффективным тарифом (Room → User → default).
# Работает поблочно по chunk_size — защита от OOM на 10k+ записях.
# Progress сохраняется в recalc_jobs.progress/processed, чтобы UI мог показывать
# живой progress-bar через polling.
# ==========================================================================

def _recalc_compute_one(db_session, reading, user, room, prev_reading, tariffs_by_active):
    """Пересчитать одно approved-показание с актуальным тарифом.

    Возвращает (new_totals_dict, new_costs_dict). НЕ пишет в БД.
    prev_reading — последнее утверждённое показание по комнате СТРОГО ДО текущего
    (для вычисления дельт; None если эта запись — первая по комнате).
    """
    from decimal import Decimal
    from app.modules.utility.services.tariff_cache import tariff_cache
    from app.modules.utility.services.calculations import calculate_utilities, D

    ZERO = Decimal("0.000")

    tariff = (
        tariff_cache.get_effective_tariff(user=user, room=room)
        or tariffs_by_active
    )

    p_hot = D(prev_reading.hot_water) if prev_reading else ZERO
    p_cold = D(prev_reading.cold_water) if prev_reading else ZERO
    p_elect = D(prev_reading.electricity) if prev_reading else ZERO

    hot_corr = D(reading.hot_correction or 0)
    cold_corr = D(reading.cold_correction or 0)
    elect_corr = D(reading.electricity_correction or 0)
    sewage_corr = D(reading.sewage_correction or 0)

    d_hot = max(ZERO, (D(reading.hot_water) - p_hot) - hot_corr)
    d_cold = max(ZERO, (D(reading.cold_water) - p_cold) - cold_corr)

    residents = Decimal(user.residents_count or 1)
    total_room = Decimal(room.total_room_residents if room.total_room_residents and room.total_room_residents > 0 else 1)
    d_elect = max(ZERO, ((residents / total_room) * (D(reading.electricity) - p_elect)) - elect_corr)

    costs = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=d_hot, volume_cold=d_cold,
        volume_sewage=max(ZERO, (d_hot + d_cold) - sewage_corr),
        volume_electricity_share=d_elect,
    )

    cost_205 = costs["cost_social_rent"]
    cost_209 = costs["total_cost"] - cost_205

    # При пересчёте debt_209/205 и overpayment_209/205 НЕ трогаем —
    # они пришли из предыдущего периода и не зависят от текущего тарифа.
    # Adjustments тоже не учитываем в total — они применяются в момент
    # первичного approve. Если админ хочет «чистый» пересчёт по тарифу —
    # ему важны именно cost_* поля и total_cost без корректировок долга.
    total_209 = cost_209 + (reading.debt_209 or Decimal("0")) - (reading.overpayment_209 or Decimal("0"))
    total_205 = cost_205 + (reading.debt_205 or Decimal("0")) - (reading.overpayment_205 or Decimal("0"))

    new_fields = {
        "total_209": total_209,
        "total_205": total_205,
        "total_cost": total_209 + total_205,
    }
    for k, v in costs.items():
        new_fields[k] = v
    return new_fields


def _recalc_run(job_id: int, apply: bool):
    """Общая логика для preview и apply. Разница — пишем ли результаты в БД.

    Идея реализации:
      * один проход по всем approved readings периода, батчами по 500;
      * для каждой записи считаем новые поля, сравниваем total_cost;
      * собираем агрегат: increased/decreased/unchanged + топ-30 по |delta|;
      * при apply=True — обновляем записи чанком через bulk_update_mappings.
    """
    from decimal import Decimal
    from sqlalchemy.orm import selectinload, load_only
    from app.modules.utility.models import RecalcJob, MeterReading, BillingPeriod, Tariff, Room, User

    CHUNK = 500

    with sync_db_session() as db:
        job = db.query(RecalcJob).filter(RecalcJob.id == job_id).first()
        if not job:
            logger.error(f"[RECALC] job_id={job_id} not found")
            return {"status": "error", "error": "job_not_found"}

        if job.status == "cancelled":
            logger.info(f"[RECALC] job {job_id} cancelled before start")
            return {"status": "cancelled"}

        try:
            # Фиксируем запущенный статус
            job.status = "apply_pending" if apply else "preview_pending"
            job.progress = 0
            job.processed = 0
            db.commit()

            period = db.query(BillingPeriod).filter(BillingPeriod.id == job.period_id).first()
            if not period:
                raise ValueError(f"Период id={job.period_id} не найден")

            # Берём любой активный тариф как fallback — вдруг ни user, ни room
            # не указывают эффективный тариф.
            fallback_tariff = (
                db.query(Tariff).filter(Tariff.is_active).order_by(Tariff.id).first()
            )
            if not fallback_tariff:
                raise ValueError("Нет ни одного активного тарифа — пересчёт невозможен")

            total_q = db.query(MeterReading).filter(
                MeterReading.period_id == period.id,
                MeterReading.is_approved.is_(True),
            )
            total = total_q.count()
            job.total_readings = total
            db.commit()

            if total == 0:
                job.status = "preview_ready" if not apply else "done"
                job.progress = 100
                job.diff_summary = {
                    "total": 0, "unchanged": 0, "increased": 0, "decreased": 0,
                    "sum_old": "0.00", "sum_new": "0.00", "delta": "0.00", "top": [],
                }
                if apply:
                    job.applied_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.commit()
                return {"status": job.status, "total": 0}

            unchanged = increased = decreased = 0
            sum_old = Decimal("0")
            sum_new = Decimal("0")
            top_diffs = []  # [(abs_delta, dict_item)]

            offset = 0
            while offset < total:
                # Важно: readings — это ORM-объекты, user+room подгружаем eager
                # чтобы внутри чанка не было N+1.
                chunk = (
                    db.query(MeterReading)
                    .options(
                        selectinload(MeterReading.user).selectinload(User.room),
                    )
                    .filter(
                        MeterReading.period_id == period.id,
                        MeterReading.is_approved.is_(True),
                    )
                    .order_by(MeterReading.id)
                    .offset(offset)
                    .limit(CHUNK)
                    .all()
                )
                if not chunk:
                    break

                # Соберём prev-reading для всех комнат в чанке одним запросом.
                # Для пересчёта нужен последний approved reading по комнате
                # строго ДО created_at текущего. Аккуратно — в рамках одного
                # периода берём «предыдущий» по created_at в пределах комнаты.
                updates = []
                for r in chunk:
                    user = r.user
                    room = user.room if user else None
                    if not user or not room:
                        # ломаные данные — пропускаем
                        continue

                    prev = (
                        db.query(MeterReading)
                        .filter(
                            MeterReading.room_id == room.id,
                            MeterReading.is_approved.is_(True),
                            MeterReading.created_at < r.created_at,
                        )
                        .order_by(MeterReading.created_at.desc())
                        .first()
                    )

                    new_fields = _recalc_compute_one(db, r, user, room, prev, fallback_tariff)

                    old_total = Decimal(str(r.total_cost or 0))
                    new_total = Decimal(str(new_fields["total_cost"] or 0))
                    delta = new_total - old_total
                    sum_old += old_total
                    sum_new += new_total

                    if delta == 0:
                        unchanged += 1
                    elif delta > 0:
                        increased += 1
                    else:
                        decreased += 1

                    # Поддерживаем отсортированный топ по |delta|, размер <=30
                    if delta != 0:
                        item = {
                            "reading_id": r.id,
                            "user_id": user.id,
                            "username": user.username,
                            "room": f"{room.dormitory_name}, {room.room_number}" if room else "",
                            "old_total": str(old_total),
                            "new_total": str(new_total),
                            "delta": str(delta),
                        }
                        top_diffs.append((abs(delta), item))
                        # Каждые 100 сравнений уменьшаем хвост — экономия памяти.
                        if len(top_diffs) > 200:
                            top_diffs.sort(key=lambda x: x[0], reverse=True)
                            top_diffs = top_diffs[:30]

                    if apply:
                        updates.append({"id": r.id, "created_at": r.created_at, **{k: v for k, v in new_fields.items()}})

                if apply and updates:
                    # Составной PK (id, created_at) требует передавать оба поля
                    # в bulk_update_mappings. SQLAlchemy сам сопоставит записи.
                    db.bulk_update_mappings(MeterReading, updates)

                offset += CHUNK
                job.processed = min(offset, total)
                job.progress = int(job.processed / total * 100) if total else 100
                db.commit()

                # Повторная проверка: админ мог отменить
                db.refresh(job)
                if job.status == "cancelled":
                    logger.info(f"[RECALC] job {job_id} cancelled mid-run")
                    db.rollback()
                    return {"status": "cancelled"}

            top_diffs.sort(key=lambda x: x[0], reverse=True)
            top_items = [item for _, item in top_diffs[:30]]

            job.diff_summary = {
                "total": total,
                "unchanged": unchanged,
                "increased": increased,
                "decreased": decreased,
                "sum_old": str(sum_old.quantize(Decimal("0.01"))),
                "sum_new": str(sum_new.quantize(Decimal("0.01"))),
                "delta": str((sum_new - sum_old).quantize(Decimal("0.01"))),
                "top": top_items,
            }
            if apply:
                job.status = "done"
                job.applied_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                job.status = "preview_ready"
            job.progress = 100
            db.commit()
            logger.info(f"[RECALC] job {job_id} finished (apply={apply}) — {total} readings")
            return {"status": job.status, "total": total}

        except Exception as exc:
            db.rollback()
            logger.exception(f"[RECALC] job {job_id} failed")
            job2 = db.query(RecalcJob).filter(RecalcJob.id == job_id).first()
            if job2:
                job2.status = "failed"
                job2.error = str(exc)[:2000]
                db.commit()
            return {"status": "failed", "error": str(exc)}


@celery.task(name="recalc_period_preview_task")
def recalc_period_preview_task(job_id: int):
    """Read-only прогон: собирает diff_summary без апдейтов MeterReading."""
    return _recalc_run(job_id, apply=False)


@celery.task(name="recalc_period_apply_task")
def recalc_period_apply_task(job_id: int):
    """Применяет пересчитанные значения к БД (bulk_update)."""
    return _recalc_run(job_id, apply=True)


# ==========================================================================
# GSHEETS CLEANUP — автоочистка старых завершённых строк импорта
# ==========================================================================
# Gsheets-буфер за годы может набрать сотни тысяч строк (у нас 2000+ жильцов
# каждый месяц подаёт показание — это ~24k строк в год). Хранить всю историю
# бесконечно нет смысла: как только строка ушла в approved/auto_approved —
# у неё есть ссылка reading_id на MeterReading, где лежат реальные данные.
# rejected-строки нужны лишь для кратковременного разбора «почему отклонил»
# и через N месяцев их тоже можно удалять.
#
# pending / conflict / unmatched НИКОГДА не удаляем автоматически — это
# строки, которые ждут решения админа.
# ==========================================================================


def _cleanup_gsheets_rows(retention_days: int) -> dict:
    """Удаляет завершённые строки старше retention_days. Батчами по 1000.

    Возвращает {'deleted': N, 'cutoff': 'ISO datetime'}.
    """
    from datetime import datetime as _dt, timedelta as _td
    from app.modules.utility.models import GSheetsImportRow

    if retention_days <= 0:
        logger.info("[GSHEETS-CLEANUP] retention_days<=0, задача пропущена")
        return {"deleted": 0, "cutoff": None, "skipped": True}

    cutoff = _dt.utcnow() - _td(days=retention_days)
    terminal_statuses = ("approved", "auto_approved", "rejected")

    CHUNK = 1000
    total_deleted = 0

    with sync_db_session() as db:
        while True:
            # Выбираем id-батч в пределах CHUNK. Идём по (status, created_at)
            # — попадаем в индекс idx_gsheets_status_created.
            ids = [
                r[0] for r in db.query(GSheetsImportRow.id)
                .filter(
                    GSheetsImportRow.created_at < cutoff,
                    GSheetsImportRow.status.in_(terminal_statuses),
                )
                .order_by(GSheetsImportRow.id)
                .limit(CHUNK)
                .all()
            ]
            if not ids:
                break

            deleted = (
                db.query(GSheetsImportRow)
                .filter(GSheetsImportRow.id.in_(ids))
                .delete(synchronize_session=False)
            )
            db.commit()
            total_deleted += deleted

            # Если пришло меньше чем CHUNK — значит больше удалять нечего.
            if deleted < CHUNK:
                break

    logger.info(
        f"[GSHEETS-CLEANUP] удалено {total_deleted} строк "
        f"(старше {retention_days} дней, cutoff={cutoff.isoformat()})"
    )
    return {
        "deleted": total_deleted,
        "cutoff": cutoff.isoformat(),
        "retention_days": retention_days,
    }


@celery.task(name="cleanup_gsheets_old_rows_task")
def cleanup_gsheets_old_rows_task(retention_days: int | None = None):
    """Ежедневная автоочистка старых импортов из Google Sheets.

    Без параметра — берёт settings.GSHEETS_CLEANUP_DAYS (дефолт 365).
    Админ может вызвать вручную с параметром через /admin/analyzer/gsheets/cleanup-now.
    """
    days = retention_days if retention_days is not None else settings.GSHEETS_CLEANUP_DAYS
    return _cleanup_gsheets_rows(days)