import openpyxl
import logging
from decimal import Decimal
from typing import Dict, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import select
from rapidfuzz import process, fuzz
from app.modules.utility.models import User, MeterReading, BillingPeriod

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 88
CHUNK_SIZE = 2000  # Размер пакета для БД


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
    """Проверяет, является ли строка Excel ФИО жильца."""
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

    if len(val.split()) < 2:
        return False

    return True


# --- ЭТА ФУНКЦИЯ НУЖНА ДЛЯ excel_service.py ---
def find_user_fuzzy(target_name: str, users_map: Dict[str, int]) -> Optional[int]:
    """
    Глобальная функция поиска (используется внешними модулями).
    В users_map ключи должны быть уже нормализованы!
    """
    if not target_name:
        return None

    norm_target = normalize_name(target_name)

    # 1. Точное совпадение
    if norm_target in users_map:
        return users_map[norm_target]

    # 2. Нечеткий поиск
    # Примечание: process.extractOne каждый раз проходит по всем ключам.
    # Это нормально для единичных вызовов из excel_service,
    # но внутри массового импорта мы используем оптимизацию (см. ниже).
    match = process.extractOne(norm_target, list(users_map.keys()), scorer=fuzz.token_sort_ratio)

    if match:
        best_match_name, score, _ = match
        if score >= FUZZY_THRESHOLD:
            return users_map[best_match_name]

    return None


def sync_import_debts_process(file_path: str, db: Session, account_type: str) -> dict:
    """
    Функция массового импорта с оптимизациями (буферизация, кэширование).
    """
    logger.info(f"Starting debts import from {file_path} for Account {account_type}")

    try:
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

        # 2. Предзагрузка пользователей
        users_raw = db.execute(select(User.id, User.username).where(User.is_deleted == False)).all()
        # Карта: нормализованное имя -> ID
        users_map: Dict[str, int] = {normalize_name(u.username): u.id for u in users_raw}
        # Список ключей один раз для rapidfuzz (оптимизация)
        users_keys = list(users_map.keys())

        # 3. Предзагрузка показаний
        readings_raw = db.execute(
            select(MeterReading.id, MeterReading.user_id)
            .where(MeterReading.period_id == active_period.id)
        ).all()
        readings_map: Dict[int, int] = {user_id: r_id for r_id, user_id in readings_raw}

        # Статистика и буферы
        stats = {
            "processed": 0, "updated": 0, "created": 0,
            "not_found_users": [], "errors": [], "account": account_type
        }
        updates_buffer: List[dict] = []
        inserts_buffer: List[MeterReading] = []

        # Локальный кэш нечеткого поиска (чтобы не искать одни и те же ФИО повторно)
        fuzzy_cache: Dict[str, Optional[int]] = {}

        # Внутренняя оптимизированная функция поиска
        def get_user_id_optimized(fio: str) -> Optional[int]:
            norm = normalize_name(fio)

            # Точное совпадение
            if norm in users_map:
                return users_map[norm]

            # Проверка кэша
            if norm in fuzzy_cache:
                return fuzzy_cache[norm]

            # Тяжелый поиск
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
        for row in worksheet.iter_rows(min_row=8, values_only=True):
            if not row or len(row) < 7:
                continue

            name_cell = row[0]
            if not is_valid_name_row(name_cell):
                continue

            fio_raw = str(name_cell).strip()
            stats["processed"] += 1

            # Используем оптимизированный поиск с кэшем
            user_id = get_user_id_optimized(fio_raw)

            if not user_id:
                stats["not_found_users"].append(fio_raw)
                continue

            debt_val = clean_decimal(row[5])
            over_val = clean_decimal(row[6])

            # 5. Подготовка данных (Update или Insert)
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

            # 6. Сброс буферов в БД (Чанкинг)
            if len(inserts_buffer) >= CHUNK_SIZE:
                db.add_all(inserts_buffer)
                db.flush()
                inserts_buffer.clear()

            if len(updates_buffer) >= CHUNK_SIZE:
                db.bulk_update_mappings(MeterReading, updates_buffer)
                updates_buffer.clear()

        # Сохранение остатков
        if inserts_buffer:
            db.add_all(inserts_buffer)
        if updates_buffer:
            db.bulk_update_mappings(MeterReading, updates_buffer)

        db.commit()
        workbook.close()

        # Уникализируем список ненайденных
        stats["not_found_users"] = list(set(stats["not_found_users"]))

        logger.info(
            f"Import finished. Processed: {stats['processed']}, Updated: {stats['updated']}, Created: {stats['created']}")
        return stats

    except Exception as error:
        db.rollback()
        logger.exception("Import failed")
        return {"status": "error", "message": f"Ошибка во время импорта: {str(error)}"}