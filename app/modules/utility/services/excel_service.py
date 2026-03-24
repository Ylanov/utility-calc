import io
import asyncio
from typing import Dict, List, Tuple
from decimal import Decimal
from openpyxl import Workbook, load_workbook
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.modules.utility.models import User, MeterReading, BillingPeriod, Room
from app.core.auth import get_password_hash
from app.modules.utility.services.debt_import import normalize_name

ZERO = Decimal("0.00")


def _load_workbook_sync(file_content: bytes):
    return load_workbook(filename=io.BytesIO(file_content), read_only=True, data_only=True)


# ======================================================
# IMPORT USERS (ПЕРЕПИСАНО ПОД ROOM)
# ======================================================

async def import_users_from_excel(file_content: bytes, db: AsyncSession) -> dict:
    try:
        workbook = await asyncio.to_thread(_load_workbook_sync, file_content)
        worksheet = workbook.active

        # Пользователи
        users_result = await db.execute(
            select(User).where(User.is_deleted.is_(False))
        )

        existing_users: Dict[str, User] = {
            normalize_name(user.username): user
            for user in users_result.scalars().all()
        }

        # Комнаты
        rooms_result = await db.execute(select(Room))
        existing_rooms: Dict[str, Room] = {
            f"{r.dormitory_name}_{r.room_number}": r
            for r in rooms_result.scalars().all()
        }

        added_count = 0
        updated_count = 0
        skipped_count = 0
        errors: List[str] = []
        password_cache: Dict[str, str] = {}

        rows = worksheet.iter_rows(min_row=2, values_only=True)

        for row_index, row in enumerate(rows, start=2):
            try:
                if not row or not row[0]:
                    skipped_count += 1
                    continue

                username = str(row[0]).strip()
                normalized_username = normalize_name(username)

                password = str(row[1]).strip() if len(row) > 1 and row[1] else username
                dormitory = str(row[2]).strip() if len(row) > 2 and row[2] else "Неизвестно"

                # 🔥 НОВОЕ: номер комнаты
                room_number = str(row[7]).strip() if len(row) > 7 and row[7] else f"Комната {username}"

                try:
                    apartment_area = Decimal(str(row[3])) if len(row) > 3 and row[3] else ZERO
                    residents_count = int(row[4]) if len(row) > 4 and row[4] else 1
                    total_room_residents = int(row[5]) if len(row) > 5 and row[5] else residents_count
                except Exception:
                    apartment_area = ZERO
                    residents_count = 1
                    total_room_residents = 1
                    errors.append(f"Строка {row_index}: Ошибка чисел.")

                workplace = str(row[6]).strip() if len(row) > 6 and row[6] else None

                # ======================================================
                # РАБОТА С КОМНАТОЙ
                # ======================================================

                room_key = f"{dormitory}_{room_number}"

                if room_key in existing_rooms:
                    room = existing_rooms[room_key]
                    room.apartment_area = apartment_area
                    room.total_room_residents = total_room_residents
                else:
                    room = Room(
                        dormitory_name=dormitory,
                        room_number=room_number,
                        apartment_area=apartment_area,
                        total_room_residents=total_room_residents
                    )
                    db.add(room)
                    await db.flush()
                    existing_rooms[room_key] = room

                # ======================================================
                # ПАРОЛЬ
                # ======================================================

                if password in password_cache:
                    hashed_password = password_cache[password]
                else:
                    hashed_password = get_password_hash(password)
                    password_cache[password] = hashed_password

                # ======================================================
                # ПОЛЬЗОВАТЕЛЬ
                # ======================================================

                if normalized_username in existing_users:
                    user = existing_users[normalized_username]

                    user.workplace = workplace
                    user.residents_count = residents_count
                    user.room_id = room.id

                    if password:
                        user.hashed_password = hashed_password

                    updated_count += 1

                else:
                    new_user = User(
                        username=username,
                        hashed_password=hashed_password,
                        role="user",
                        workplace=workplace,
                        residents_count=residents_count,
                        room_id=room.id,
                        is_deleted=False
                    )
                    db.add(new_user)
                    existing_users[normalized_username] = new_user
                    added_count += 1

            except Exception as error:
                skipped_count += 1
                errors.append(f"Строка {row_index}: {str(error)}")

        await db.commit()
        await asyncio.to_thread(workbook.close)

        return {
            "status": "success",
            "added": added_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "errors": errors
        }

    except Exception as error:
        await db.rollback()
        return {
            "status": "error",
            "message": f"Ошибка импорта: {str(error)}",
            "errors": [str(error)]
        }


# ======================================================
# EXPORT REPORT (ПЕРЕПИСАНО ПОД ROOM)
# ======================================================

async def generate_billing_report_xlsx(db: AsyncSession, period_id: int) -> Tuple[io.BytesIO, str]:
    period_result = await db.execute(select(BillingPeriod).where(BillingPeriod.id == period_id))
    period = period_result.scalars().first()

    if not period:
        raise ValueError("Период не найден")

    statement = (
        select(User, MeterReading)
        .join(MeterReading, User.id == MeterReading.user_id)
        .options(selectinload(User.room))
        .where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True)
        )
        .order_by(Room.dormitory_name, User.username)
    )

    result = await db.execute(statement)

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Сводная ведомость"

    headers = [
        "Общежитие/Комната", "ФИО", "Площадь", "Жильцов",
        "ГВС", "ХВС", "Водоотв.", "Электроэнергия",
        "Содержание", "Наем", "ТКО", "Отопление",
        "209", "205", "ИТОГО"
    ]
    worksheet.append(headers)

    total_sum = ZERO
    total_209_sum = ZERO
    total_205_sum = ZERO

    for user, reading in result:
        room = user.room

        total_cost = Decimal(reading.total_cost or 0)
        t_209 = Decimal(reading.total_209 or 0)
        t_205 = Decimal(reading.total_205 or 0)

        total_sum += total_cost
        total_209_sum += t_209
        total_205_sum += t_205

        room_display = "-"
        area = None
        residents = "-"

        if room:
            room_display = f"{room.dormitory_name} / {room.room_number}"
            area = room.apartment_area
            residents = f"{user.residents_count}/{room.total_room_residents}"

        worksheet.append([
            room_display,
            user.username,
            area,
            residents,
            reading.cost_hot_water,
            reading.cost_cold_water,
            reading.cost_sewage,
            reading.cost_electricity,
            reading.cost_maintenance,
            reading.cost_social_rent,
            reading.cost_waste,
            reading.cost_fixed_part,
            t_209,
            t_205,
            total_cost
        ])

    worksheet.append([""] * 11 + ["ИТОГО:", total_209_sum, total_205_sum, total_sum])

    output = await asyncio.to_thread(lambda: _save_workbook_sync(workbook))
    filename = f"Report_{period.name}".replace(" ", "_") + ".xlsx"

    return output, filename


def _save_workbook_sync(workbook) -> io.BytesIO:
    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
