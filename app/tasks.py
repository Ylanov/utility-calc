import os
import logging
from app.worker import celery
from app.database import SessionLocalSync
from app.models import User, MeterReading, Tariff, BillingPeriod
from app.services.pdf_generator import generate_receipt_pdf
from sqlalchemy.orm import selectinload

# Настраиваем логгер для задач Celery. Это поможет отлаживать проблемы в воркере.
logger = logging.getLogger(__name__)

# Путь для сохранения файлов, доступный и веб-контейнеру и воркеру
SHARED_STORAGE_PATH = "/app/static/generated_files"
os.makedirs(SHARED_STORAGE_PATH, exist_ok=True)


def get_sync_db():
    """Вспомогательная функция для получения синхронной сессии БД в задаче"""
    db = SessionLocalSync()
    try:
        # Эта функция yield'ит сессию, но так как мы используем ее в простом вызове,
        # return работает так же.
        return db
    finally:
        # Гарантированное закрытие сессии, чтобы не оставлять открытых соединений.
        db.close()


@celery.task(name="generate_receipt_task")
def generate_receipt_task(reading_id: int) -> dict:
    """
    Фоновая задача генерации PDF квитанции.
    Запускается Celery воркером.
    :param reading_id: ID записи показаний, для которой нужна квитанция.
    :return: Словарь с результатом: статус и путь к файлу или сообщение об ошибке.
    """
    logger.info(f"Received task to generate PDF for reading_id={reading_id}")
    db = get_sync_db()

    try:
        # 1. Получаем все необходимые данные из БД одним запросом
        # selectinload() позволяет избежать проблемы N+1, подгружая связанные user и period
        reading = db.query(MeterReading).options(
            selectinload(MeterReading.user),
            selectinload(MeterReading.period)
        ).filter(MeterReading.id == reading_id).first()

        # 2. Проверяем, что все данные на месте
        if not reading:
            logger.error(f"Reading with ID {reading_id} not found.")
            return {"status": "error", "message": "Запись показаний не найдена"}

        if not reading.user:
            logger.error(f"User for reading ID {reading_id} not found.")
            return {"status": "error", "message": "Пользователь для этой записи не найден"}

        if not reading.period:
            logger.error(f"Period for reading ID {reading_id} is not set.")
            return {"status": "error", "message": "Для этой записи не установлен расчетный период"}

        # Получаем тариф (пока что он один с id=1)
        tariff = db.query(Tariff).filter(Tariff.id == 1).first()
        if not tariff:
            logger.error("Default tariff with ID 1 not found in DB.")
            return {"status": "error", "message": "Тарифы не найдены в системе"}

        # Получаем предыдущее утвержденное показание для расчета дельты
        prev_reading = db.query(MeterReading).filter(
            MeterReading.user_id == reading.user_id,
            MeterReading.is_approved == True,
            MeterReading.created_at < reading.created_at
        ).order_by(MeterReading.created_at.desc()).first()

        # 3. Вызываем функцию-генератор PDF
        logger.info(f"Generating PDF for user '{reading.user.username}'...")
        final_path = generate_receipt_pdf(
            user=reading.user,
            reading=reading,
            period=reading.period,
            tariff=tariff,
            prev_reading=prev_reading,
            output_dir=SHARED_STORAGE_PATH  # Указываем общую папку для сохранения
        )

        # 4. ВАЖНО: Устанавливаем права доступа к файлу
        # Это нужно, чтобы пользователь 'nginx' внутри контейнера Nginx мог прочитать
        # файл, созданный пользователем 'root' (под которым работает Celery).
        # 0o644 -> rw-r--r-- (Владелец пишет, остальные читают)
        os.chmod(final_path, 0o644)

        # Получаем только имя файла из полного пути
        filename = os.path.basename(final_path)

        logger.info(f"PDF successfully generated at: {final_path}")

        # 5. Возвращаем успешный результат
        return {"status": "done", "filename": filename, "path": final_path}

    except Exception as e:
        # Логируем полное исключение для отладки
        logger.exception(f"An unexpected error occurred in generate_receipt_task for reading_id={reading_id}")
        return {"status": "error", "message": "Произошла внутренняя ошибка при создании файла."}

