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
    if not value:
        return ""
    return " ".join(str(value).lower().split())

def is_valid_name_row(cell_value: str) -> bool:
    if not cell_value:
        return False
    val = str(cell_value).strip()
    lower_val = val.lower()
    stop_words = ["договор", "закрыт", "итого", "счет", "контрагенты", "сальдо", "выводимые", "единица", "обороты", "дебет", "кредит"]
    for word in stop_words:
        if word in lower_val:
            return False
    if len(val.split()) < 2:
        return False
    return True

def find_user_fuzzy(target_name: str, users_map: Dict[str, int]) -> Optional[int]:
    norm_target = normalize_name(target_name)
    if norm_target in users_map:
        return users_map[norm_target]
    match = process.extractOne(norm_target, users_map.keys(), scorer=fuzz.token_sort_ratio)
    if match:
        best_match_name, score, _ = match
        if score >= FUZZY_THRESHOLD:
            return users_map[best_match_name]
    return None

def sync_import_debts_process(file_path: str, db: Session, account_type: str) -> dict:
    logger.info(f"Starting debts import from {file_path} for Account {account_type}")
    try:
        workbook = openpyxl.load_workbook(filename=file_path, read_only=True, data_only=True)
        worksheet = workbook.active
    except Exception as error:
        logger.exception("Failed to open Excel file")
        return {"status": "error", "message": str(error)}

    try:
        active_period = db.execute(select(BillingPeriod).where(BillingPeriod.is_active.is_(True))).scalars().first()
        if not active_period:
            return {"status": "error", "message": "Нет активного периода для загрузки долгов"}

        users_raw = db.execute(select(User.id, User.username)).all()
        users_map: Dict[str, int] = {normalize_name(username): user_id for user_id, username in users_raw}

        readings_raw = db.execute(select(MeterReading.id, MeterReading.user_id).where(MeterReading.period_id == active_period.id)).all()
        readings_map: Dict[int, int] = {user_id: reading_id for reading_id, user_id in readings_raw}

        stats = {"processed": 0, "updated": 0, "created": 0, "not_found_users": [], "errors": [], "account": account_type}
        updates_buffer: List[dict] = []
        inserts_buffer: List[MeterReading] = []

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

            debt_val = clean_decimal(row[5])
            over_val = clean_decimal(row[6])

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
                    # Инициализируем нулями поля другого счета
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

        if inserts_buffer:
            db.add_all(inserts_buffer)
            db.flush()
        if updates_buffer:
            db.bulk_update_mappings(MeterReading, updates_buffer)

        db.commit()
        workbook.close()
        logger.info(f"Import finished. Processed: {stats['processed']}, Updated: {stats['updated']}, Created: {stats['created']}")
        return stats

    except Exception as error:
        db.rollback()
        logger.exception("Import failed")
        return {"status": "error", "message": str(error)}