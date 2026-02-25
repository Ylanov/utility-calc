import openpyxl
import logging
from decimal import Decimal
from typing import Dict, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import select
from rapidfuzz import process, fuzz
from app.models import User, MeterReading, BillingPeriod

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 88
CHUNK_SIZE = 2000  # Размер пакета для работы с БД при больших объемах данных


def clean_decimal(value) -> Decimal:
    """Очищает и конвертирует значение из Excel в Decimal."""
    if value is None:
        return Decimal("0.00")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.replace(" ", "").replace("\xa0", "").replace(",", ".")
        try:
            return Decimal(cleaned)
        except Exception:
            return Decimal("0.00")
    return Decimal("0.00")


def normalize_name(value: str) -> str:
    """Приводит ФИО к единому нижнему регистру без лишних пробелов."""
    if not value:
        return ""
    return " ".join(str(value).lower().split())


def is_valid_name_row(cell_value: str) -> bool:
    """Проверяет, является ли строка Excel ФИО жильца (отсеивает заголовки и итоги)."""
    if not cell_value:
        return False
    val = str(cell_value).strip()
    lower_val = val.lower()

    stop_words = [
        "договор", "закрыт", "итого", "счет", "контрагенты",
        "сальдо", "выводимые", "единица", "обороты", "дебет", "кредит"
    ]
    for word in stop_words:
        if word in lower_val:
            return False

    # ФИО должно состоять минимум из двух слов (Фамилия и Имя/Инициалы)
    if len(val.split()) < 2:
        return False

    return True


def sync_import_debts_process(file_path: str, db: Session, account_type: str) -> dict:
    """Синхронная функция для импорта долгов из 1С в фоне (Celery)."""
    logger.info(f"Starting debts import from {file_path} for Account {account_type}")

    try:
        # data_only=True гарантирует чтение значений, а не формул
        workbook = openpyxl.load_workbook(filename=file_path, read_only=True, data_only=True)
        worksheet = workbook.active
    except Exception as error:
        logger.exception("Failed to open Excel file")
        return {"status": "error", "message": f"Ошибка чтения файла: {str(error)}"}

    try:
        # 1. Получаем активный период
        active_period = db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
        ).scalars().first()

        if not active_period:
            return {"status": "error", "message": "Нет активного периода для загрузки долгов"}

        # 2. Предзагрузка пользователей для быстрого поиска в памяти
        users_raw = db.execute(select(User.id, User.username).where(User.is_deleted == False)).all()
        users_map: Dict[str, int] = {normalize_name(username): user_id for user_id, username in users_raw}
        users_keys = list(users_map.keys())  # Создаем список ключей ОДИН раз для RapidFuzz

        # 3. Предзагрузка существующих показаний (черновиков) в текущем периоде
        readings_raw = db.execute(
            select(MeterReading.id, MeterReading.user_id)
            .where(MeterReading.period_id == active_period.id)
        ).all()
        readings_map: Dict[int, int] = {user_id: reading_id for reading_id, user_id in readings_raw}

        # Статистика и буферы
        stats = {
            "processed": 0, "updated": 0, "created": 0,
            "not_found_users": [], "errors": [], "account": account_type
        }
        updates_buffer: List[dict] = []
        inserts_buffer: List[MeterReading] = []

        # Кэш для нечеткого поиска (чтобы не искать одни и те же опечатки дважды)
        fuzzy_cache: Dict[str, Optional[int]] = {}

        # Внутренняя функция для быстрого поиска с кэшированием
        def get_user_id(fio: str) -> Optional[int]:
            norm = normalize_name(fio)

            # Точное совпадение O(1)
            if norm in users_map:
                return users_map[norm]

            # Проверка в кэше O(1)
            if norm in fuzzy_cache:
                return fuzzy_cache[norm]

            # Тяжелый нечеткий поиск (только для новых опечаток)
            match = process.extractOne(norm, users_keys, scorer=fuzz.token_sort_ratio)
            if match:
                best_match_name, score, _ = match
                if score >= FUZZY_THRESHOLD:
                    found_id = users_map[best_match_name]
                    fuzzy_cache[norm] = found_id
                    return found_id

            fuzzy_cache[norm] = None
            return None

        # 4. Чтение строк Excel
        # Начинаем с 8 строки, так как сверху в 1С обычно шапка
        for row in worksheet.iter_rows(min_row=8, values_only=True):
            if not row or len(row) < 7:
                continue

            name_cell = row[0]
            if not is_valid_name_row(name_cell):
                continue

            fio_raw = str(name_cell).strip()
            stats["processed"] += 1

            user_id = get_user_id(fio_raw)

            if not user_id:
                stats["not_found_users"].append(fio_raw)
                continue

            # Парсим деньги (в 1С Дебет - долг, Кредит - переплата)
            debt_val = clean_decimal(row[5])
            over_val = clean_decimal(row[6])

            # 5. Обновление или создание записи
            if user_id in readings_map:
                r_id = readings_map[user_id]
                update_data = {"id": r_id}

                if account_type == "209":
                    update_data["debt_209"] = debt_val
                    update_data["overpayment_209"] = over_val
                elif account_type == "205":
                    update_data["debt_205"] = debt_val
                    update_data["overpayment_205"] = over_val

                updates_buffer.append(update_data)
                stats["updated"] += 1
            else:
                new_reading = MeterReading(
                    user_id=user_id,
                    period_id=active_period.id,
                    is_approved=False,
                    debt_209=Decimal("0.00"), overpayment_209=Decimal("0.00"),
                    debt_205=Decimal("0.00"), overpayment_205=Decimal("0.00")
                )

                if account_type == "209":
                    new_reading.debt_209 = debt_val
                    new_reading.overpayment_209 = over_val
                elif account_type == "205":
                    new_reading.debt_205 = debt_val
                    new_reading.overpayment_205 = over_val

                inserts_buffer.append(new_reading)
                stats["created"] += 1

            # 6. Пакетное сохранение в БД (Chunking) для защиты оперативной памяти
            if len(inserts_buffer) >= CHUNK_SIZE:
                db.add_all(inserts_buffer)
                db.flush()
                inserts_buffer.clear()

            if len(updates_buffer) >= CHUNK_SIZE:
                db.bulk_update_mappings(MeterReading, updates_buffer)
                updates_buffer.clear()

        # Сохраняем остатки в буфере
        if inserts_buffer:
            db.add_all(inserts_buffer)
        if updates_buffer:
            db.bulk_update_mappings(MeterReading, updates_buffer)

        db.commit()
        workbook.close()

        # Убираем дубликаты из списка ненайденных
        stats["not_found_users"] = list(set(stats["not_found_users"]))

        logger.info(
            f"Import finished. Processed: {stats['processed']}, Updated: {stats['updated']}, Created: {stats['created']}")
        return stats

    except Exception as error:
        db.rollback()
        logger.exception("Import failed")
        return {"status": "error", "message": f"Ошибка во время импорта: {str(error)}"}