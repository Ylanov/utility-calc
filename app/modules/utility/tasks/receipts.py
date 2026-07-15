# Квитанции: генерация PDF одной квитанции и массовый ZIP-архив периода.
# Вербатим-перенос из монолитного tasks.py (строки 44-302), поведение 1:1.

import os
import shutil
import zipfile
import tempfile
from datetime import datetime, timezone

from sqlalchemy.orm import selectinload

from app.worker import celery
from app.modules.utility.models import MeterReading, Tariff, BillingPeriod, Adjustment, User
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.s3_client import s3_service
from app.modules.utility.services.reading_calculator import is_meaningful_prev

from ._shared import logger, sync_db_session


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
