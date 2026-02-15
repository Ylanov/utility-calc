import io
from typing import Dict, List, Tuple
from decimal import Decimal

from openpyxl import Workbook, load_workbook
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models import User, MeterReading, BillingPeriod
from app.auth import get_password_hash


ZERO = Decimal("0.00")


async def import_users_from_excel(file_content: bytes, db: AsyncSession) -> dict:
    try:
        workbook = load_workbook(
            filename=io.BytesIO(file_content),
            read_only=True,
            data_only=True
        )
        worksheet = workbook.active

        users_result = await db.execute(select(User))
        existing_users: Dict[str, User] = {
            user.username: user
            for user in users_result.scalars().all()
        }

        added_count = 0
        updated_count = 0
        skipped_count = 0
        errors: List[str] = []
        new_users: List[User] = []
        password_cache: Dict[str, str] = {}

        rows = worksheet.iter_rows(min_row=2, values_only=True)

        for row_index, row in enumerate(rows, start=2):
            try:
                if not row or not row[0]:
                    skipped_count += 1
                    continue

                username = str(row[0]).strip()
                if not username:
                    skipped_count += 1
                    continue

                password = str(row[1]).strip() if len(row) > 1 and row[1] else username
                dormitory = str(row[2]).strip() if len(row) > 2 and row[2] else None

                try:
                    apartment_area = Decimal(str(row[3])) if len(row) > 3 and row[3] else ZERO
                    residents_count = int(row[4]) if len(row) > 4 and row[4] else 1
                    total_room_residents = int(row[5]) if len(row) > 5 and row[5] else residents_count
                except Exception:
                    apartment_area = ZERO
                    residents_count = 1
                    total_room_residents = 1
                    errors.append(f"Строка {row_index}: Ошибка числовых значений")

                workplace = str(row[6]).strip() if len(row) > 6 and row[6] else None

                if password in password_cache:
                    hashed_password = password_cache[password]
                else:
                    hashed_password = get_password_hash(password)
                    password_cache[password] = hashed_password

                if username in existing_users:
                    user = existing_users[username]
                    user.dormitory = dormitory
                    user.apartment_area = apartment_area
                    user.residents_count = residents_count
                    user.total_room_residents = total_room_residents
                    user.workplace = workplace
                    if password:
                        user.hashed_password = hashed_password
                    updated_count += 1
                else:
                    new_user = User(
                        username=username,
                        hashed_password=hashed_password,
                        role="user",
                        dormitory=dormitory,
                        apartment_area=apartment_area,
                        residents_count=residents_count,
                        total_room_residents=total_room_residents,
                        workplace=workplace
                    )
                    new_users.append(new_user)
                    existing_users[username] = new_user
                    added_count += 1

            except Exception as error:
                skipped_count += 1
                errors.append(f"Строка {row_index}: {str(error)}")

        if new_users:
            db.add_all(new_users)

        await db.commit()
        workbook.close()

        return {
            "status": "success",
            "message": "Импорт завершен",
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
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [str(error)]
        }


async def generate_billing_report_xlsx(
    db: AsyncSession,
    period_id: int
) -> Tuple[io.BytesIO, str]:

    period_result = await db.execute(
        select(BillingPeriod).where(BillingPeriod.id == period_id)
    )
    period = period_result.scalars().first()

    if not period:
        raise ValueError("Период не найден")

    statement = (
        select(User, MeterReading)
        .join(MeterReading, User.id == MeterReading.user_id)
        .where(
            MeterReading.period_id == period_id,
            MeterReading.is_approved.is_(True)
        )
        .order_by(User.dormitory, User.username)
    )

    result = await db.execute(statement)

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Сводная ведомость"

    headers = [
        "Общежитие/Комната",
        "ФИО (Логин)",
        "Площадь",
        "Жильцов",
        "ГВС (руб)",
        "ХВС (руб)",
        "Водоотв. (руб)",
        "Электроэнергия (руб)",
        "Содержание (руб)",
        "Наем (руб)",
        "ТКО (руб)",
        "Отопление + ОДН (руб)",
        "ИТОГО (руб)"
    ]

    worksheet.append(headers)

    total_sum = ZERO

    for user, reading in result:
        total_cost = Decimal(reading.total_cost or 0)
        total_sum += total_cost

        worksheet.append([
            user.dormitory,
            user.username,
            user.apartment_area,
            f"{user.residents_count}/{user.total_room_residents}",
            reading.cost_hot_water,
            reading.cost_cold_water,
            reading.cost_sewage,
            reading.cost_electricity,
            reading.cost_maintenance,
            reading.cost_social_rent,
            reading.cost_waste,
            reading.cost_fixed_part,
            total_cost
        ])

    worksheet.append([""] * 11 + ["ИТОГО:", total_sum])

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)

    filename = f"Report_{period.name}".replace(" ", "_") + ".xlsx"

    return output, filename
