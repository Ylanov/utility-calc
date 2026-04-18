# app/modules/utility/services/debt_import.py
import openpyxl
import logging
from decimal import Decimal
from typing import Dict, Optional
from sqlalchemy.orm import Session
from sqlalchemy import select
from rapidfuzz import process, fuzz
from app.modules.utility.models import User, MeterReading, BillingPeriod

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 88


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


def find_user_fuzzy(target_name: str, users_map: Dict[str, int]) -> Optional[int]:
    """Глобальная функция поиска (используется внешними модулями)."""
    if not target_name:
        return None

    norm_target = normalize_name(target_name)

    if norm_target in users_map:
        return users_map[norm_target]

    match = process.extractOne(norm_target, list(users_map.keys()), scorer=fuzz.token_sort_ratio)

    if match:
        best_match_name, score, _ = match
        if score >= FUZZY_THRESHOLD:
            return users_map[best_match_name]

    return None


def sync_import_debts_process(file_path: str, db: Session, account_type: str) -> dict:
    """
    Функция массового импорта долгов.
    Долг из 1С привязывается к КОМНАТЕ жильца. Долги соседей по комнате суммируются.

    Важно: ВСЁ делается одной транзакцией. Если на 5000-й строке случится
    ошибка — откатываются все 4999 предыдущих, файл не удаляется (пусть
    админ исправит и перезапустит).
    """
    logger.info(f"Starting debts import from {file_path} for Account {account_type}")

    try:
        workbook = openpyxl.load_workbook(filename=file_path, read_only=True, data_only=True)
        worksheet = workbook.active
    except Exception as error:
        logger.exception("Failed to open Excel file")
        # Бросаем — Celery должен увидеть падение и ретраить.
        raise RuntimeError(f"Ошибка чтения файла: {error}") from error

    try:
        active_period = db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
        ).scalars().first()

        if not active_period:
            return {"status": "error", "message": "Нет активного периода для загрузки долгов"}

        # 1. Предзагрузка пользователей (сохраняем id и room_id)
        users_raw = db.execute(select(User).where(User.is_deleted.is_(False))).scalars().all()
        users_map = {normalize_name(u.username): {"id": u.id, "room_id": u.room_id} for u in users_raw}
        users_keys = list(users_map.keys())

        # 2. Предзагрузка показаний (черновиков) по КОМНАТАМ
        readings_raw = db.execute(
            select(MeterReading).where(MeterReading.period_id == active_period.id)
        ).scalars().all()

        readings_map = {r.room_id: r for r in readings_raw if r.room_id is not None}

        stats = {
            "processed": 0, "updated": 0, "created": 0,
            "not_found_users": [], "errors": [], "account": account_type
        }

        updates_dict = {}  # reading_id -> reading object (для обновления)
        inserts_dict = {}  # room_id -> reading object (для вставки)
        fuzzy_cache = {}
        processed_rooms = set()  # Для обнуления старых долгов комнаты перед прибавлением новых из 1С

        def get_user_data_optimized(fio: str):
            norm = normalize_name(fio)
            if norm in users_map:
                return users_map[norm]
            if norm in fuzzy_cache:
                return fuzzy_cache[norm]

            match = process.extractOne(norm, users_keys, scorer=fuzz.token_sort_ratio)
            if match:
                best_match_name, score, _ = match
                if score >= FUZZY_THRESHOLD:
                    found_data = users_map[best_match_name]
                    fuzzy_cache[norm] = found_data
                    return found_data

            fuzzy_cache[norm] = None
            return None

        # 3. Чтение строк Excel
        for row in worksheet.iter_rows(min_row=8, values_only=True):
            if not row or len(row) < 7:
                continue

            name_cell = row[0]
            if not is_valid_name_row(name_cell):
                continue

            fio_raw = str(name_cell).strip()
            stats["processed"] += 1

            user_data = get_user_data_optimized(fio_raw)

            if not user_data or not user_data["room_id"]:
                stats["not_found_users"].append(fio_raw)
                continue

            user_id = user_data["id"]
            room_id = user_data["room_id"]

            debt_val = clean_decimal(row[5])
            over_val = clean_decimal(row[6])

            # 4. Если для этой комнаты уже есть черновик в БД
            if room_id in readings_map:
                reading = readings_map[room_id]

                # Если мы первый раз встречаем эту комнату в файле 1С - сбрасываем старые долги в 0
                if room_id not in processed_rooms:
                    if account_type == "209":
                        reading.debt_209 = Decimal("0.00")
                        reading.overpayment_209 = Decimal("0.00")
                    elif account_type == "205":
                        reading.debt_205 = Decimal("0.00")
                        reading.overpayment_205 = Decimal("0.00")
                    processed_rooms.add(room_id)
                    updates_dict[reading.id] = reading

                # ПРИБАВЛЯЕМ долги (если в 1С несколько жильцов из одной комнаты, долги просуммируются)
                if account_type == "209":
                    reading.debt_209 += debt_val
                    reading.overpayment_209 += over_val
                elif account_type == "205":
                    reading.debt_205 += debt_val
                    reading.overpayment_205 += over_val

                stats["updated"] += 1

            # 5. Если черновика в БД нет, но мы его уже создали в памяти в цикле
            elif room_id in inserts_dict:
                reading = inserts_dict[room_id]
                if account_type == "209":
                    reading.debt_209 += debt_val
                    reading.overpayment_209 += over_val
                elif account_type == "205":
                    reading.debt_205 += debt_val
                    reading.overpayment_205 += over_val

                stats["updated"] += 1

            # 6. Если черновика нет вообще - создаем новый
            else:
                new_reading = MeterReading(
                    user_id=user_id,  # Первый встреченный жилец становится номинальным автором черновика
                    room_id=room_id,
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

                inserts_dict[room_id] = new_reading
                processed_rooms.add(room_id)
                stats["created"] += 1

        # 7. Сохраняем в БД
        if inserts_dict:
            db.add_all(list(inserts_dict.values()))

        if updates_dict:
            updates_list = []
            for r in updates_dict.values():
                updates_list.append({
                    "id": r.id,
                    "debt_209": r.debt_209,
                    "overpayment_209": r.overpayment_209,
                    "debt_205": r.debt_205,
                    "overpayment_205": r.overpayment_205
                })
            db.bulk_update_mappings(MeterReading, updates_list)

        db.commit()

        stats["not_found_users"] = list(set(stats["not_found_users"]))
        logger.info(
            "Import finished. Processed: %s, Updated: %s, Created: %s",
            stats["processed"], stats["updated"], stats["created"],
        )
        return stats

    except Exception as error:
        # Полный откат: ни одна строка не должна остаться полузакоммиченной.
        db.rollback()
        logger.exception("Import failed — full rollback applied")
        # Пробрасываем — Celery отправит в retry (см. retry_kwargs у задачи).
        raise RuntimeError(f"Ошибка во время импорта: {error}") from error

    finally:
        # workbook.close() — даже при исключении. Иначе файл-дескриптор висит.
        try:
            workbook.close()
        except Exception:
            pass
