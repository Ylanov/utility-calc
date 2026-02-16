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


def clean_decimal(value) -> Decimal:
    """Безопасное преобразование значения ячейки в Decimal."""
    if value is None:
        return Decimal("0.00")

    if isinstance(value, (int, float)):
        return Decimal(str(value))

    if isinstance(value, str):
        cleaned = (
            value.replace(" ", "")
            .replace("\xa0", "")
            .replace(",", ".")
        )
        try:
            return Decimal(cleaned)
        except Exception:
            return Decimal("0.00")

    return Decimal("0.00")


def normalize_name(value: str) -> str:
    """Нормализация имени для поиска (нижний регистр, удаление лишних пробелов)."""
    if not value:
        return ""
    return " ".join(str(value).lower().split())


def is_valid_name_row(cell_value: str) -> bool:
    """Проверка, является ли строка строкой с данными пользователя, а не заголовком или мусором."""
    if not cell_value:
        return False

    val = str(cell_value).strip()
    lower_val = val.lower()

    stop_words = [
        "договор",
        "закрыт",
        "итого",
        "счет",
        "контрагенты",
        "сальдо",
        "выводимые",
        "единица",
        "обороты",
        "дебет",
        "кредит"
    ]

    for word in stop_words:
        if word in lower_val:
            return False

    if len(val.split()) < 2:
        return False

    return True


def find_user_fuzzy(target_name: str, users_map: Dict[str, int]) -> Optional[int]:
    """Нечеткий поиск пользователя по нормализованному имени."""
    norm_target = normalize_name(target_name)

    # 1. Точное совпадение (быстро)
    if norm_target in users_map:
        return users_map[norm_target]

    # 2. Нечеткое совпадение (медленнее, но надежнее для опечаток)
    match = process.extractOne(
        norm_target,
        users_map.keys(),
        scorer=fuzz.token_sort_ratio
    )

    if match:
        best_match_name, score, _ = match
        if score >= FUZZY_THRESHOLD:
            # logger.info(f"[FUZZY MATCH] '{target_name}' -> '{best_match_name}' ({score}%)")
            return users_map[best_match_name]

    return None


def sync_import_debts_process(file_path: str, db: Session, account_type: str) -> dict:
    """
    Процесс импорта долгов из Excel.

    :param file_path: Путь к временному файлу Excel.
    :param db: Сессия базы данных (синхронная).
    :param account_type: Тип счета ('209' - коммуналка, '205' - найм).
    """
    logger.info(f"Starting debts import from {file_path} for Account {account_type}")

    try:
        workbook = openpyxl.load_workbook(
            filename=file_path,
            read_only=True,
            data_only=True
        )
        worksheet = workbook.active
    except Exception as error:
        logger.exception("Failed to open Excel file")
        return {"status": "error", "message": str(error)}

    try:
        # 1. Получаем активный период (блокируем строку от изменений на время чтения, если нужно, но для bulk это не критично)
        active_period = (
            db.execute(
                select(BillingPeriod)
                .where(BillingPeriod.is_active.is_(True))
            )
            .scalars()
            .first()
        )

        if not active_period:
            return {
                "status": "error",
                "message": "Нет активного периода для загрузки долгов"
            }

        # 2. Кэшируем пользователей и существующие записи показаний для скорости
        users_raw = db.execute(
            select(User.id, User.username)
        ).all()

        users_map: Dict[str, int] = {
            normalize_name(username): user_id
            for user_id, username in users_raw
        }

        readings_raw = db.execute(
            select(MeterReading.id, MeterReading.user_id)
            .where(MeterReading.period_id == active_period.id)
        ).all()

        # Словарь user_id -> meter_reading_id
        readings_map: Dict[int, int] = {
            user_id: reading_id
            for reading_id, user_id in readings_raw
        }

        stats = {
            "processed": 0,
            "updated": 0,
            "created": 0,
            "not_found_users": [],
            "errors": [],
            "account": account_type
        }

        # Буферы для массовых операций
        updates_buffer: List[dict] = []
        inserts_buffer: List[MeterReading] = []

        # 3. Итерируемся по Excel
        # Пропускаем первые строки заголовка (обычно в 1С ОСВ данные начинаются не с 1 строки)
        # min_row=8 - эмпирическое значение, можно настроить
        for row in worksheet.iter_rows(min_row=8, values_only=True):
            if not row or len(row) < 7:
                continue

            name_cell = row[0]

            if not is_valid_name_row(name_cell):
                continue

            fio_raw = str(name_cell).strip()
            stats["processed"] += 1

            user_id = find_user_fuzzy(fio_raw, users_map)

            if not user_id:
                stats["not_found_users"].append(fio_raw)
                continue

            # Читаем Долг (Col 5 / F) и Переплату (Col 6 / G)
            debt_val = clean_decimal(row[5])
            over_val = clean_decimal(row[6])

            # Логика разделения по счетам
            if user_id in readings_map:
                # Запись уже есть -> добавляем в список на обновление
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
                # Записи нет -> создаем объект для вставки
                new_reading = MeterReading(
                    user_id=user_id,
                    period_id=active_period.id,
                    is_approved=False
                )

                if account_type == "209":
                    new_reading.debt_209 = debt_val
                    new_reading.overpayment_209 = over_val
                elif account_type == "205":
                    new_reading.debt_205 = debt_val
                    new_reading.overpayment_205 = over_val

                inserts_buffer.append(new_reading)
                stats["created"] += 1

        # 4. Выполняем массовые операции в БД (Bulk Operations)

        # Сначала вставляем новые записи
        if inserts_buffer:
            db.add_all(inserts_buffer)
            # flush нужен, чтобы новые объекты получили ID (если бы мы их дальше использовали, но тут просто сохраняем)
            db.flush()

            # Затем массово обновляем существующие (очень быстро для 5000+ записей)
        if updates_buffer:
            db.bulk_update_mappings(MeterReading, updates_buffer)

        db.commit()
        workbook.close()

        logger.info(
            f"Import finished for Account {account_type}. "
            f"Processed: {stats['processed']}, "
            f"Updated: {stats['updated']}, "
            f"Created: {stats['created']}, "
            f"Not found: {len(stats['not_found_users'])}"
        )

        return stats

    except Exception as error:
        db.rollback()
        logger.exception("Import failed")
        return {
            "status": "error",
            "message": str(error)
        }