# Анализаторы: арсенал (фрод-паттерны) и детект аномалий одного показания.
# Вербатим-перенос из tasks.py (строки 786-843), поведение 1:1.

from sqlalchemy.orm import selectinload

from app.worker import celery
from app.modules.utility.models import MeterReading, User

from ._shared import logger, sync_db_session


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
