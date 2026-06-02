# app/modules/utility/tasks.py
import os
import shutil
import zipfile
import logging
import tempfile
import asyncio
from datetime import datetime, timezone

from app.core.time_utils import utcnow
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from redis import asyncio as aioredis
from fastapi_cache import FastAPICache
from fastapi_cache.backends.redis import RedisBackend
from redis import Redis

from app.worker import celery
# sync_db_session жил исторически тут. Перенесли в app.core.database, чтобы
# tariff_cache мог импортировать его без circular dep (см. инцидент мая 2026:
# tariff_cache хотел `from app.core.database import sync_db_session`, но
# функция была только здесь → ImportError → cache навсегда пустой → все
# promote_auto_approved падали с no_active_tariff). Реэкспортируем имя для
# обратной совместимости — старые импорты `from app.modules.utility.tasks
# import sync_db_session` продолжают работать.
from app.core.database import SessionLocalSync, sync_db_session  # noqa: F401
from app.core.config import settings
from app.modules.utility.models import MeterReading, Tariff, BillingPeriod, Adjustment, SystemSetting, User
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.debt_import import sync_import_debts_process
from app.modules.utility.services.s3_client import s3_service
from app.modules.utility.services.billing import close_current_period
from app.modules.utility.services.reading_calculator import is_meaningful_prev

logger = logging.getLogger(__name__)


def get_sync_db():
    # Сохранён для обратной совместимости. В новом коде используй sync_db_session().
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
    # Изолированная директория с правами 700 — Sonar python:S5443
    # («publicly writable /tmp»). Чистится в finally независимо от того,
    # вернулся ли таск нормально или поднял исключение.
    temp_dir = tempfile.mkdtemp(prefix="utility_pdf_")
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

            # Поиск prev по period_id (детерминированно) + пропуск reading'ов
            # с обнулёнными значениями (AUTO_GENERATED / DATA_OVERFLOW_RESET /
            # MANUAL_RECEIPT) — их hot/cold/elect = 0 даёт фантастическую дельту
            # при следующей реальной подаче (см. is_meaningful_prev).
            prev_candidates = (
                db.query(MeterReading)
                .filter(
                    MeterReading.room_id == room.id,
                    MeterReading.is_approved.is_(True),
                    MeterReading.period_id < (reading.period_id or 0),
                )
                .order_by(MeterReading.period_id.desc())
                .limit(20)
                .all()
            )
            prev_reading = next(
                (c for c in prev_candidates if is_meaningful_prev(c)),
                None,
            )

            adjustments = (
                db.query(Adjustment)
                .filter(
                    Adjustment.user_id == user.id,
                    Adjustment.period_id == period.id
                )
                .all()
            )

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
    finally:
        # На S3-success файла уже нет (os.remove выше); на S3-fail он
        # перемещён shutil.move в static. В обоих случаях dir пуста или
        # содержит остатки при exception — rmtree всё прибирает.
        shutil.rmtree(temp_dir, ignore_errors=True)


# ======================================================================
# REMINDERS (push-уведомления о подаче показаний)
# ======================================================================
@celery.task(name="remind_submit_readings_task", queue="default")
def remind_submit_readings_task():
    """Ежедневная beat-задача: push-напоминание жильцам о подаче показаний.

    Логика триггеров (`days_left = submission_end_day - today.day`):
      - 3 дня до конца окна → "Через 3 дня закрывается приём"
      - 1 день  → "Завтра последний день"
      - 0 дней  → "Сегодня последний день"
      - в прочие дни задача выходит без рассылки

    Кому шлём: жильцам с role='user', не is_deleted, привязанным к комнате,
    у которых ЕЩЁ НЕТ MeterReading в активном периоде, и есть хотя бы один
    DeviceToken (т.е. установлено мобильное приложение).
    """
    from app.modules.utility.models import DeviceToken
    from app.modules.utility.services.notification_service import send_push_to_tokens_sync
    from sqlalchemy import delete as sa_delete

    logger.info("[REMIND] Checking submission deadline...")
    try:
        with sync_db_session() as db:
            import calendar as _calendar
            start_setting = db.query(SystemSetting).filter_by(key="submission_start_day").first()
            end_setting = db.query(SystemSetting).filter_by(key="submission_end_day").first()
            start_day = int(start_setting.value) if start_setting else 15
            end_day = int(end_setting.value) if end_setting else 3

            # datetime.now() здесь возвращает локальное время worker'а;
            # в docker-compose Celery работает с timezone="Europe/Moscow"
            # (см. app/worker.py), а beat-расписание тоже в МСК — значит
            # day-of-month сравнивается без сюрпризов на стыках суток.
            today = datetime.now()
            # Сколько дней до закрытия окна — с учётом перехода через месяц
            # (start > end, напр. 15 → 3 следующего). В «хвосте» текущего месяца
            # конец — в следующем: days_left = (дней до конца месяца) + end_day.
            days_in_month = _calendar.monthrange(today.year, today.month)[1]
            if start_day <= end_day:
                days_left = end_day - today.day
            elif today.day >= start_day:
                days_left = (days_in_month - today.day) + end_day
            elif today.day <= end_day:
                days_left = end_day - today.day
            else:
                days_left = -1  # сегодня вне окна подачи

            if days_left not in (3, 1, 0):
                logger.info(
                    f"[REMIND] today.day={today.day} window={start_day}-{end_day} "
                    f"days_left={days_left} — not a reminder day, skip"
                )
                return {"sent": 0, "skipped": True, "days_left": days_left}

            active = db.query(BillingPeriod).filter_by(is_active=True).first()
            if not active:
                logger.info("[REMIND] No active period — skip")
                return {"sent": 0, "skipped": True, "reason": "no_active_period"}

            # NOT EXISTS: жильцы у которых нет показаний в активном периоде.
            # Postgres превращает это в anti-join по составному индексу
            # idx_reading_user_approved (есть в perf_001_scaling_indexes).
            mr_subq = (
                db.query(MeterReading.id)
                .filter(
                    MeterReading.user_id == User.id,
                    MeterReading.period_id == active.id,
                )
                .exists()
            )
            user_ids = [
                row[0]
                for row in db.query(User.id).filter(
                    User.role == "user",
                    User.is_deleted.is_(False),
                    User.room_id.is_not(None),
                    ~mr_subq,
                ).all()
            ]
            if not user_ids:
                logger.info("[REMIND] All eligible users have submitted — nothing to do")
                return {"sent": 0, "skipped": True, "reason": "everyone_submitted"}

            tokens = [
                t[0]
                for t in db.query(DeviceToken.token)
                .filter(DeviceToken.user_id.in_(user_ids))
                .all()
            ]
            if not tokens:
                logger.info(
                    f"[REMIND] {len(user_ids)} users miss readings, "
                    f"but none have device tokens registered"
                )
                return {
                    "sent": 0,
                    "users_without_readings": len(user_ids),
                    "tokens": 0,
                }

            if days_left == 3:
                title = "📋 Напоминание о показаниях"
                body = "Через 3 дня закрывается приём показаний. Подайте через приложение."
            elif days_left == 1:
                title = "⏰ Завтра последний день"
                body = "Завтра — последний день для подачи показаний за этот месяц."
            else:  # 0
                title = "🔔 Сегодня последний день"
                body = "Сегодня — последний день для подачи показаний. Не забудьте!"

            result = send_push_to_tokens_sync(
                tokens=tokens,
                title=title,
                body=body,
                data={"type": "submission_reminder", "days_left": str(days_left)},
            )

            # Чистим невалидные FCM-токены — UnregisteredError означает что
            # приложение удалено или токен переиздан. Иначе будем стабильно
            # тратить квоту FCM на мёртвые токены.
            if result["invalid_tokens"]:
                db.execute(
                    sa_delete(DeviceToken).where(
                        DeviceToken.token.in_(result["invalid_tokens"])
                    )
                )
                db.commit()
                logger.info(
                    f"[REMIND] Removed {len(result['invalid_tokens'])} stale device tokens"
                )

            logger.info(
                f"[REMIND] days_left={days_left} "
                f"users_without_readings={len(user_ids)} tokens={len(tokens)} "
                f"success={result['success']} failed={result['failed']}"
            )
            return {
                "days_left": days_left,
                "users_without_readings": len(user_ids),
                "tokens_sent": len(tokens),
                "success": result["success"],
                "failed": result["failed"],
            }
    except Exception:
        logger.exception("[REMIND] Task failed")
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

        # Импорт tariff_cache вынесен из горячего цикла (раньше делался на каждой
        # квитанции). Сам кеш Singleton — повторный import дешёвый, но синтаксический
        # шум в hot path неприятен.
        from app.modules.utility.services.tariff_cache import tariff_cache

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

                    # ОПТИМИЗАЦИЯ N+1 (apr 2026): раньше внутри цикла делалось
                    # 2 запроса на КАЖДУЮ квитанцию (Adjustment + prev MeterReading).
                    # На 1000 квитанций = 2000 round-trip'ов до Postgres.
                    # Теперь preload одним батчем на весь chunk:
                    #   - Adjustments по (user_id IN chunk_users, period_id == period)
                    #   - prev_readings по (room_id IN chunk_rooms, is_approved, asc by created_at)
                    chunk_user_ids = list({r.user_id for r in readings})
                    chunk_room_ids = list({r.room_id for r in readings if r.room_id})

                    adjustments_by_user: dict[int, list] = {}
                    if chunk_user_ids:
                        for adj in db.query(Adjustment).filter(
                            Adjustment.user_id.in_(chunk_user_ids),
                            Adjustment.period_id == period_id,
                        ).all():
                            adjustments_by_user.setdefault(adj.user_id, []).append(adj)

                    # Все approved readings для нужных комнат — в Python для каждого r
                    # ищем последний по period_id (детерминированно). Сортировка
                    # period_id, created_at, id гарантирует стабильный порядок при
                    # повторных вызовах. Пропускаем reading'и с обнулёнными значениями
                    # (AUTO_GENERATED, DATA_OVERFLOW_RESET, MANUAL_RECEIPT) — см.
                    # is_meaningful_prev.
                    readings_by_room: dict[int, list] = {}
                    if chunk_room_ids:
                        for mr in db.query(MeterReading).filter(
                            MeterReading.room_id.in_(chunk_room_ids),
                            MeterReading.is_approved.is_(True),
                        ).order_by(
                            MeterReading.room_id,
                            MeterReading.period_id,
                            MeterReading.created_at,
                            MeterReading.id,
                        ).all():
                            readings_by_room.setdefault(mr.room_id, []).append(mr)

                    for r in readings:
                        try:
                            adjustments = adjustments_by_user.get(r.user_id, [])

                            # Предыдущее показание: последний approved в той же комнате
                            # СТРОГО ДО r.period_id, с пропуском синтетических.
                            prev_reading = None
                            r_pid = r.period_id or 0
                            for cand in reversed(readings_by_room.get(r.room_id, [])):
                                if (cand.period_id or 0) >= r_pid:
                                    continue
                                if not is_meaningful_prev(cand):
                                    continue
                                prev_reading = cand
                                break

                            # Через единый кеш: Room.tariff_id → User.tariff_id → default
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


@celery.task(name="auto_fill_missing_readings_task")
def auto_fill_missing_readings_task() -> dict:
    """Bug AO: дневная авто-добивка пропущенных reading'ов по нормативу.

    Каждый день в 03:45 (см. worker.py beat_schedule) проходит по
    периодам, для которых должна была быть подача (любой период старше
    чем `min_age_days`, кроме самого свежего активного — там жильцы
    ещё могут подать), и для каждого жильца БЕЗ reading'а в этом
    периоде создаёт reading через billing.auto_fill_period_readings:
      - AUTO_NORM_SANCTION × коэф — после 3 пропусков подряд
      - AUTO_AVG — среднее по дельтам manual-подач
      - AUTO_AVG_FALLBACK — повтор последних показаний
      - AUTO_NO_HISTORY — только фикс-часть тарифа

    Параметры через analyzer_settings:
      - billing.auto_fill_enabled (bool, default True) — глобальный switch
      - billing.auto_fill_min_age_days (int, default 0) — отсечка молодых
        периодов (0 = обрабатываем все, даже активные с прошедшим
        окном подачи; при 7 — только периоды старше недели)
      - billing.auto_fill_max_periods (int, default 12) — лимит на прогон

    Идемпотентна — повторный запуск не создаёт дубликатов
    (auto_fill_period_readings проверяет существующие reading'и).
    """
    import asyncio
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from app.modules.utility.models import BillingPeriod
    from app.modules.utility.services.analyzer_config import config
    from app.modules.utility.services.billing import auto_fill_period_readings

    enabled = config.get_bool("billing.auto_fill_enabled", True)
    if not enabled:
        logger.info("[AUTO-FILL] disabled via analyzer_settings, skipping")
        return {"status": "disabled"}

    min_age_days = config.get_int("billing.auto_fill_min_age_days", 0)
    max_periods = config.get_int("billing.auto_fill_max_periods", 12)

    async def _run():
        async_engine = create_async_engine(
            settings.DATABASE_URL_ASYNC,
            echo=False, future=True, pool_pre_ping=True,
            connect_args={"prepared_statement_cache_size": 0, "statement_cache_size": 0, "command_timeout": 60},
        )
        smaker = sessionmaker(bind=async_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
        try:
            async with smaker() as db:
                cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=min_age_days)
                periods = (await db.execute(
                    select(BillingPeriod)
                    .where(BillingPeriod.created_at < cutoff)
                    .order_by(BillingPeriod.id.desc())
                    .limit(max_periods)
                )).scalars().all()
                results = []
                for p in periods:
                    try:
                        r = await auto_fill_period_readings(db, p.id, dry_run=False)
                        if r.get("created", 0) > 0:
                            logger.info(
                                "[AUTO-FILL] period=%s id=%s created=%d by=%s",
                                p.name, p.id, r["created"], r.get("by_strategy"),
                            )
                            results.append({"period_id": p.id, "name": p.name, "created": r["created"]})
                    except Exception as e:
                        logger.exception("[AUTO-FILL] period=%s failed: %s", p.name, e)
                return {
                    "status": "ok",
                    "periods_checked": len(periods),
                    "periods_filled": len(results),
                    "total_created": sum(r["created"] for r in results),
                    "details": results,
                }
        finally:
            await async_engine.dispose()

    return asyncio.run(_run())


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
            # Дефолт — московский стандарт 15 → 3 следующего месяца.
            start_day = int(start_setting.value) if start_setting else 15
            end_day = int(end_setting.value) if end_setting else 3
            today = datetime.now()
            current_day = today.day
            active = db.query(BillingPeriod).filter_by(is_active=True).first()

            month_names = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                           "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

            # Окно подачи может ПЕРЕХОДИТЬ через границу месяца (start > end,
            # напр. 15 → 3 следующего). in_window — открыт ли приём сегодня.
            if start_day <= end_day:
                in_window = start_day <= current_day <= end_day
            else:
                in_window = current_day >= start_day or current_day <= end_day

            if in_window:
                if not active:
                    # В «хвосте» wrap-окна (1..end_day) приём идёт за ПРОШЛЫЙ
                    # календарный месяц → период именуем прошлым месяцем.
                    if start_day > end_day and current_day <= end_day:
                        pm = today.month - 1 if today.month > 1 else 12
                        py = today.year if today.month > 1 else today.year - 1
                    else:
                        pm, py = today.month, today.year
                    period_name = f"{month_names[pm]} {py}"
                    exists = db.query(BillingPeriod).filter_by(name=period_name).first()
                    if not exists:
                        new_period = BillingPeriod(name=period_name, is_active=True)
                        db.add(new_period)
                        db.commit()
                        logger.info(f"[AUTO] Opened new period: {period_name}")
            elif active:
                # Сегодня ВНЕ окна подачи, но период активен.
                # КРИТИЧНО (фикс июнь 2026): закрываем ТОЛЬКО когда окно уже
                # ЗАКОНЧИЛОСЬ (мы ПОСЛЕ end_day), а НЕ до его открытия. Для
                # невраппинг-окна (start<=end, напр. 15→28) дни 1..start-1 — это
                # ещё ДО начала приёма; закрытие там выставляло бы ВСЕМ 0 (никто
                # не подавал). Для враппинг-окна (start>end, 15→3) любой день вне
                # окна уже после end — там закрывать корректно.
                window_has_ended = (start_day > end_day) or (current_day > end_day)
                if not window_has_ended:
                    logger.info(
                        "[AUTO] Вне окна, но ДО открытия приёма (день %d < start %d) — "
                        "период '%s' НЕ закрываем (иначе всем 0).",
                        current_day, start_day, active.name)
                else:
                    # ИСПРАВЛЕНИЕ (apr 2026): только наблюдательная проверка lock
                    # (read-only get), реальный atomic lock делает сама
                    # close_period_task.
                    redis_client = Redis.from_url(settings.REDIS_URL)
                    if redis_client.get("lock:close_period"):
                        logger.info("[AUTO] Close already running, skip duplicate")
                    else:
                        admin = db.query(User).filter_by(username="admin").first()
                        if admin:
                            close_period_task.delay(admin.id)
                            logger.info(f"[AUTO] Triggered closing task for period '{active.name}'")
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


@celery.task(name="run_arsenal_analyzer_task", queue="arsenal_gsm_default")
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

def _recalc_compute_one(db_session, reading, user, room, prev_reading, tariffs_by_active,
                        global_heating_on: bool = True,
                        global_hw_on: bool = True):
    """Пересчитать одно approved-показание с актуальным тарифом.

    Возвращает (new_totals_dict, new_costs_dict). НЕ пишет в БД.
    prev_reading — последнее утверждённое показание по комнате СТРОГО ДО текущего
    (для вычисления дельт; None если эта запись — первая по комнате).
    global_heating_on / global_hw_on — глобальные SystemSetting (emergency override).
    Per-tariff поля (heating_active, heating_season_start/end и т.п.) — берутся
    из выбранного tariff внутри функции через is_*_active_now().
    """
    from decimal import Decimal
    from app.modules.utility.services.tariff_cache import tariff_cache
    from app.modules.utility.services.calculations import calculate_utilities, D

    ZERO = Decimal("0.000")

    tariff = (
        tariff_cache.get_effective_tariff(user=user, room=room)
        or tariffs_by_active
    )

    # BASELINE: первая подача жильца — потребление = 0, но area-based
    # (содержание/найм/ТКО/отопление) ПЛАТЯТСЯ ВСЕГДА. Bug L (фикс
    # may 2026): раньше тут возвращались сплошные нули — area-based
    # начисления ~5000-7000 ₽/мес теряли все жильцы с AUTO_GENERATED
    # baseline. Теперь вызываем calculate_utilities с volume_*=0:
    # water/sewage = 0 (правильно), area-based = area × tariff.
    if prev_reading is None:
        from app.modules.utility.services.calculations import CalculationError as _CE
        try:
            baseline = calculate_utilities(
                user=user, room=room, tariff=tariff,
                volume_hot=ZERO, volume_cold=ZERO,
                volume_sewage=ZERO, volume_electricity_share=ZERO,
                heating_season_active=(global_heating_on and tariff.is_heating_active_now()),
                hot_water_heating_active=(global_hw_on and tariff.is_hw_heating_active_now()),
            )
            base_total = Decimal(str(baseline.get("total_cost") or 0))
            base_205 = Decimal(str(baseline.get("cost_social_rent") or 0))
            base_209 = base_total - base_205
        except _CE as _exc:
            logger.warning(
                "[recalc] baseline calc_utilities failed reading_id=%s: %s",
                reading.id, _exc,
            )
            baseline = {
                "cost_hot_water": ZERO, "cost_cold_water": ZERO, "cost_sewage": ZERO,
                "cost_electricity": ZERO, "cost_maintenance": ZERO, "cost_social_rent": ZERO,
                "cost_waste": ZERO, "cost_fixed_part": ZERO, "total_cost": ZERO,
            }
            base_total = base_205 = base_209 = ZERO

        # Долг/переплата 1С НЕ в ИТОГО (30.05.2026) — только начисление.
        total_209_b = base_209
        total_205_b = base_205
        return {
            "total_209": total_209_b,
            "total_205": total_205_b,
            "total_cost": total_209_b + total_205_b,
            "cost_hot_water": Decimal(str(baseline.get("cost_hot_water") or 0)),
            "cost_cold_water": Decimal(str(baseline.get("cost_cold_water") or 0)),
            "cost_sewage": Decimal(str(baseline.get("cost_sewage") or 0)),
            "cost_electricity": Decimal(str(baseline.get("cost_electricity") or 0)),
            "cost_maintenance": Decimal(str(baseline.get("cost_maintenance") or 0)),
            "cost_social_rent": Decimal(str(baseline.get("cost_social_rent") or 0)),
            "cost_waste": Decimal(str(baseline.get("cost_waste") or 0)),
            "cost_fixed_part": Decimal(str(baseline.get("cost_fixed_part") or 0)),
        }

    p_hot = D(prev_reading.hot_water)
    p_cold = D(prev_reading.cold_water)
    p_elect = D(prev_reading.electricity)

    hot_corr = D(reading.hot_correction or 0)
    cold_corr = D(reading.cold_correction or 0)
    elect_corr = D(reading.electricity_correction or 0)
    sewage_corr = D(reading.sewage_correction or 0)

    d_hot = max(ZERO, (D(reading.hot_water) - p_hot) - hot_corr)
    d_cold = max(ZERO, (D(reading.cold_water) - p_cold) - cold_corr)

    residents = Decimal(user.residents_count or 1)
    total_room = Decimal(room.total_room_residents if room.total_room_residents and room.total_room_residents > 0 else 1)
    d_elect = max(ZERO, ((residents / total_room) * (D(reading.electricity) - p_elect)) - elect_corr)

    # global flags AND per-tariff (heating_active, season_start/end в самом tariff)
    _heating = global_heating_on and tariff.is_heating_active_now()
    _hw = global_hw_on and tariff.is_hw_heating_active_now()
    costs = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=d_hot, volume_cold=d_cold,
        volume_sewage=max(ZERO, (d_hot + d_cold) - sewage_corr),
        volume_electricity_share=d_elect,
        heating_season_active=_heating,
        hot_water_heating_active=_hw,
    )

    cost_205 = costs["cost_social_rent"]
    cost_209 = costs["total_cost"] - cost_205

    # Санитарный потолок: если пересчёт даёт нереалистичную сумму
    # (> MAX_TOTAL_COST_PER_READING, обычно 100k ₽/период) — НЕ обновляем,
    # возвращаем исходные значения и логируем. Это страховка от bug-инцидентов
    # (см. валидатор reading_validators.py — там 1.48 млрд ₽-инцидент).
    from app.modules.utility.services.reading_validators import validate_total_cost
    _sanity = validate_total_cost(costs["total_cost"])
    if not _sanity.ok:
        logger.warning(
            "[recalc] reading_id=%s skipped: %s (computed total=%s, kept old)",
            reading.id, "; ".join(_sanity.errors), costs["total_cost"],
        )
        return {
            "total_209": reading.total_209 or Decimal("0"),
            "total_205": reading.total_205 or Decimal("0"),
            "total_cost": reading.total_cost or Decimal("0"),
        }

    # При пересчёте debt_209/205 и overpayment_209/205 НЕ трогаем —
    # они пришли из предыдущего периода и не зависят от текущего тарифа.
    # Adjustments тоже не учитываем в total — они применяются в момент
    # первичного approve. Если админ хочет «чистый» пересчёт по тарифу —
    # ему важны именно cost_* поля и total_cost без корректировок долга.
    # Долг/переплата 1С НЕ в ИТОГО (30.05.2026) — только начисление.
    total_209 = cost_209
    total_205 = cost_205

    # Whitelist полей которые реально есть в MeterReading. calculate_utilities
    # возвращает helper-поля типа sanity_warning (для UI), которые нельзя
    # передавать в update().values() — SQLAlchemy ругается Unconsumed column.
    # Раньше bulk_update_mappings молча игнорировал лишние ключи — после
    # перехода на explicit update() пришлось делать whitelist явно.
    _COST_KEYS = (
        "cost_hot_water", "cost_cold_water", "cost_sewage", "cost_electricity",
        "cost_maintenance", "cost_social_rent", "cost_waste", "cost_fixed_part",
    )
    new_fields = {
        "total_209": total_209,
        "total_205": total_205,
        "total_cost": total_209 + total_205,
    }
    for k in _COST_KEYS:
        if k in costs:
            new_fields[k] = costs[k]
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
    from sqlalchemy.orm import selectinload
    from app.modules.utility.models import RecalcJob, MeterReading, BillingPeriod, Tariff, User

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

            # Сезонные флаги читаем ОДИН раз перед обходом всех reading'ов.
            # При перерасчёте 5000 квитанций без этого было бы 5000 SELECT'ов
            # за SystemSetting. compute использует тот же набор флагов что
            # и /api/calculate, иначе recalc находил бы ложный «дрейф».
            from app.modules.utility.routers.settings import load_seasonal_sync
            _seasonal = load_seasonal_sync(db)

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

                # ОПТИМИЗАЦИЯ N+1 (apr 2026): раньше для каждой записи в chunk
                # делался отдельный SELECT на prev MeterReading по паре
                # (user_id, room_id). На 5000 readings = 5000 round-trip'ов до БД.
                # Теперь один запрос за весь chunk, in-memory поиск prev.
                #
                # ДЕТЕРМИНИЗМ (may 2026): сортировка ИСКЛЮЧИТЕЛЬНО по
                # period_id + created_at + id (стабильный порядок). Раньше
                # сортировка была только по created_at — при readings с
                # одинаковым timestamp порядок плыл между вызовами,
                # «Перерасчёт» давал разные суммы при одном и том же тарифе.
                chunk_user_ids = list({r.user_id for r in chunk if r.user_id})
                chunk_room_ids = list({r.room_id for r in chunk if r.room_id})

                prev_by_pair: dict[tuple[int, int], list] = {}
                if chunk_user_ids and chunk_room_ids:
                    for mr in db.query(MeterReading).filter(
                        MeterReading.user_id.in_(chunk_user_ids),
                        MeterReading.room_id.in_(chunk_room_ids),
                        MeterReading.is_approved.is_(True),
                    ).order_by(
                        MeterReading.user_id,
                        MeterReading.room_id,
                        MeterReading.period_id,
                        MeterReading.created_at,
                        MeterReading.id,
                    ).all():
                        prev_by_pair.setdefault((mr.user_id, mr.room_id), []).append(mr)

                updates = []
                for r in chunk:
                    user = r.user
                    room = user.room if user else None
                    if not user or not room:
                        # ломаные данные — пропускаем
                        continue

                    # prev ищется ПО ПАРЕ (user_id, room_id), по period_id (а не
                    # created_at — иначе recalc недетерминирован). Пропускаем
                    # synth-reading'и (AUTO_GENERATED/DATA_OVERFLOW_RESET/MANUAL_RECEIPT)
                    # — их обнулённые значения дают фантастическую дельту при
                    # следующей реальной подаче. См. is_meaningful_prev.
                    prev = None
                    r_pid = r.period_id or 0
                    for cand in reversed(prev_by_pair.get((r.user_id, r.room_id), [])):
                        if (cand.period_id or 0) >= r_pid:
                            continue
                        if not is_meaningful_prev(cand):
                            continue
                        prev = cand
                        break

                    # Per-tariff внутри _recalc_compute_one — там tariff
                    # выбирается через tariff_cache для каждой строки,
                    # поэтому seasonal-логику применяем там же.
                    new_fields = _recalc_compute_one(
                        db, r, user, room, prev, fallback_tariff,
                        global_heating_on=_seasonal.heating_season_active,
                        global_hw_on=_seasonal.hot_water_heating_active,
                    )

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
                            "room": room.format_address if room else "",
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
                    # ИСПРАВЛЕНИЕ (may 2026): раньше использовался
                    # db.bulk_update_mappings(MeterReading, updates) с
                    # передачей составного PK (id, created_at). Но
                    # MeterReading партиционирована по created_at, и
                    # bulk_update тихо возвращал rowcount=0 — admin
                    # жал «Перерасчёт» 5 раз и каждый раз видел те же
                    # 29 изменений (apply не писал, повторный preview
                    # снова обнаруживал расхождение).
                    #
                    # Now: explicit per-row UPDATE по id (SERIAL уникален
                    # сам по себе, без created_at). Чуть медленнее (500
                    # round-trips на chunk), но apply делается раз в
                    # сутки админом — нагрузка приемлема. И главное —
                    # ТОЧНО пишет, плюс логируем rowcount для отладки.
                    from sqlalchemy import update as _sa_update
                    total_affected = 0
                    for upd in updates:
                        rid = upd["id"]
                        values = {
                            k: v for k, v in upd.items()
                            if k not in ("id", "created_at")
                        }
                        res = db.execute(
                            _sa_update(MeterReading)
                            .where(MeterReading.id == rid)
                            .values(**values)
                        )
                        total_affected += res.rowcount or 0
                    logger.info(
                        "[RECALC] apply chunk: requested=%d affected=%d job=%d",
                        len(updates), total_affected, job_id,
                    )

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
    from datetime import timedelta as _td
    from app.modules.utility.models import GSheetsImportRow

    if retention_days <= 0:
        logger.info("[GSHEETS-CLEANUP] retention_days<=0, задача пропущена")
        return {"deleted": 0, "cutoff": None, "skipped": True}

    cutoff = utcnow() - _td(days=retention_days)
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


# =====================================================================
# OUTLIER READINGS AUTO-CLEANUP
#
# Sync-эквивалент app/scripts/cleanup_anomaly_readings.py (которые админ
# запускал вручную) — теперь раз в сутки автоматически через celery beat.
# Помечает readings с нереалистичными значениями как DATA_OVERFLOW_RESET
# (is_approved=False, total=0, anomaly_score=100) — admin_notifications.py
# их подсчитывает в категорию data_overflow_resets, админ видит в bell.
# =====================================================================

def _cleanup_outlier_readings_run() -> dict:
    """Sync-версия для celery worker (без asyncio)."""
    from decimal import Decimal as _D
    from sqlalchemy import or_, update as _upd
    from app.modules.utility.services.reading_validators import (
        MAX_WATER_METER_VALUE,
        MAX_ELECTRICITY_METER_VALUE,
        MAX_TOTAL_COST_PER_READING,
    )
    from app.modules.utility.models import GSheetsImportRow as _GR

    with sync_db_session() as db:
        # Только approved (черновики и так требуют разбора админа).
        # Раньше отчищенные DATA_OVERFLOW_RESET — уже не approved → не попадут.
        outliers = db.query(MeterReading).filter(
            MeterReading.is_approved.is_(True),
            or_(
                MeterReading.hot_water > MAX_WATER_METER_VALUE,
                MeterReading.cold_water > MAX_WATER_METER_VALUE,
                MeterReading.electricity > MAX_ELECTRICITY_METER_VALUE,
                MeterReading.total_cost > MAX_TOTAL_COST_PER_READING,
            ),
        ).all()

        if not outliers:
            return {"status": "ok", "readings_reset": 0, "sheet_rows_reopened": 0}

        ids = [r.id for r in outliers]
        zero = _D("0.00")
        for r in outliers:
            r.total_cost = zero
            r.total_209 = zero
            r.total_205 = zero
            r.is_approved = False
            r.anomaly_flags = "DATA_OVERFLOW_RESET"
            r.anomaly_score = 100
            db.add(r)

        # GSheets-строки, привязанные к этим readings — возвращаем в conflict.
        sheet_res = db.execute(
            _upd(_GR)
            .where(_GR.reading_id.in_(ids))
            .values(
                reading_id=None,
                status="conflict",
                processed_at=None,
                conflict_reason=(
                    "auto_cleanup_data_overflow: показания или итог превысили "
                    "санитарные пороги. Проверьте формат — возможно пропущена "
                    "десятичная точка в показании счётчика."
                ),
            )
        )
        sheet_count = sheet_res.rowcount or 0
        db.commit()

        logger.warning(
            "[CLEANUP-OUTLIER] reset=%d sheet_rows_reopened=%d readings_ids=%s",
            len(outliers), sheet_count, ids[:20],
        )
        return {
            "status": "ok",
            "readings_reset": len(outliers),
            "sheet_rows_reopened": sheet_count,
            "reading_ids": ids[:50],
        }


@celery.task(name="cleanup_outlier_readings_task")
def cleanup_outlier_readings_task():
    """Ежедневная автозачистка outlier readings.

    Запускается раз в сутки (см. worker.beat_schedule). Помечает readings с
    нереалистичными значениями (вода >MAX_WATER_METER_VALUE, электричество >
    MAX_ELECTRICITY_METER_VALUE, итог > MAX_TOTAL_COST_PER_READING) как
    DATA_OVERFLOW_RESET — админ потом разберёт через bell-уведомления.

    Связано с фиксом sanity-check в save-точках (admin_readings_*, gsheets_sync,
    tasks._recalc_compute_one) — там новые подачи блокируются сразу, а эта
    задача чистит уже сохранённые «исторические» outliers.
    """
    return _cleanup_outlier_readings_run()


@celery.task(name="scan_resident_problems_task")
def scan_resident_problems_task():
    """Фоновый скан реальных проблем жильцов → таблица resident_problems.

    Запускается по расписанию (см. worker.beat_schedule). Прогоняет детекторы
    (не подаёт / долг растёт / битый формат / замер счётчика и т.д.), upsert'ит
    персистентные сигналы, авто-закрывает исчезнувшие. Результат видят админы
    в колокольчике / Inbox / дневном брифинге.
    """
    async def _run():
        # Свой engine на вызов (как auto_fill_missing_readings_task): asyncio.run
        # создаёт новый event loop каждый запуск, а asyncpg-коннекты привязаны
        # к loop'у создания. Модульный AsyncSessionLocal на QueuePool дал бы
        # «Future attached to a different loop» при USE_PGBOUNCER=False.
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession as _AS
        from sqlalchemy.orm import sessionmaker as _smaker
        from app.modules.utility.services.resident_problem_scanner import (
            scan_resident_problems,
        )
        _engine = create_async_engine(
            settings.DATABASE_URL_ASYNC,
            echo=False, future=True, pool_pre_ping=True,
            connect_args={"prepared_statement_cache_size": 0,
                          "statement_cache_size": 0, "command_timeout": 60},
        )
        _mk = _smaker(bind=_engine, class_=_AS, expire_on_commit=False, autoflush=False)
        try:
            async with _mk() as db:
                return await scan_resident_problems(db)
        finally:
            await _engine.dispose()

    try:
        result = asyncio.run(_run())
        logger.info("[scan_resident_problems_task] %s", result)
        return result
    except Exception as e:
        logger.exception("[scan_resident_problems_task] crashed")
        return {"crashed": True, "error": str(e)}


@celery.task(name="auto_recalc_drift_task")
def auto_recalc_drift_task():
    """Авто-перерасчёт расхождений активного периода: безопасные drift фиксит,
    опасные/повторные — сигналит в Монитор проблем (RECALC_DRIFT)."""
    async def _run():
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession as _AS
        from sqlalchemy.orm import sessionmaker as _smaker
        from sqlalchemy import select as _select
        from app.modules.utility.models import BillingPeriod as _BP
        from app.modules.utility.services.auto_recalc_drift import auto_recalc_drift
        _engine = create_async_engine(
            settings.DATABASE_URL_ASYNC,
            echo=False, future=True, pool_pre_ping=True,
            connect_args={"prepared_statement_cache_size": 0,
                          "statement_cache_size": 0, "command_timeout": 120},
        )
        _mk = _smaker(bind=_engine, class_=_AS, expire_on_commit=False, autoflush=False)
        try:
            async with _mk() as db:
                period = (await db.execute(
                    _select(_BP).where(_BP.is_active.is_(True))
                )).scalars().first()
                if not period:
                    return {"skipped": "no_active_period"}
                return await auto_recalc_drift(db, period.id)
        finally:
            await _engine.dispose()

    try:
        result = asyncio.run(_run())
        logger.info("[auto_recalc_drift_task] %s", result)
        return result
    except Exception as e:
        logger.exception("[auto_recalc_drift_task] crashed")
        return {"crashed": True, "error": str(e)}
