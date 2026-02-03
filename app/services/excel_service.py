import io
from openpyxl import Workbook, load_workbook
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.models import User, MeterReading, BillingPeriod
from app.auth import get_password_hash


async def import_users_from_excel(file_content: bytes, db: AsyncSession):
    """
    Чтение Excel файла и создание пользователей.
    Ожидаемые колонки: username, password, dormitory, apartment_area, residents_count
    """
    wb = load_workbook(filename=io.BytesIO(file_content), read_only=True)
    ws = wb.active

    added_count = 0
    errors = []

    # Пропускаем заголовок (первая строка)
    rows = ws.iter_rows(min_row=2, values_only=True)

    for idx, row in enumerate(rows, start=2):
        # Ожидаем порядок:
        # 0: Логин (обязательно)
        # 1: Пароль (если нет, будет равен логину)
        # 2: Общежитие/Комната
        # 3: Площадь
        # 4: Кол-во жильцов (плательщик)
        # 5: Всего в комнате

        if not row or not row[0]:
            continue

        username = str(row[0]).strip()

        # Проверка на существование
        existing = await db.execute(select(User).where(User.username == username))
        if existing.scalars().first():
            errors.append(f"Строка {idx}: Пользователь {username} уже существует")
            continue

        password = str(row[1]).strip() if (len(row) > 1 and row[1]) else username
        dormitory = str(row[2]).strip() if (len(row) > 2 and row[2]) else None

        try:
            area = float(row[3]) if (len(row) > 3 and row[3]) else 0.0
            residents = int(row[4]) if (len(row) > 4 and row[4]) else 1
            total_residents = int(row[5]) if (len(row) > 5 and row[5]) else residents
        except ValueError:
            errors.append(f"Строка {idx}: Ошибка числовых данных")
            continue

        new_user = User(
            username=username,
            hashed_password=get_password_hash(password),
            role="user",
            dormitory=dormitory,
            apartment_area=area,
            residents_count=residents,
            total_room_residents=total_residents
        )
        db.add(new_user)
        added_count += 1

    await db.commit()
    return {"added": added_count, "errors": errors}


async def generate_billing_report_xlsx(db: AsyncSession, period_id: int):
    """
    Генерация сводного отчета в формате XLSX для бухгалтерии.
    """
    # 1. Получаем период
    p_res = await db.execute(select(BillingPeriod).where(BillingPeriod.id == period_id))
    period = p_res.scalars().first()
    period_name = period.name if period else "Unknown"

    # 2. Получаем данные
    stmt = (
        select(User, MeterReading)
        .join(MeterReading, User.id == MeterReading.user_id)
        .where(MeterReading.period_id == period_id, MeterReading.is_approved == True)
        .order_by(User.dormitory, User.username)
    )
    result = await db.execute(stmt)

    # 3. Создаем Excel
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводная ведомость"

    # Заголовок
    headers = [
        "Общежитие/Комната", "ФИО (Логин)", "Площадь", "Жильцов",
        "ГВС (руб)", "ХВС (руб)", "Водоотв. (руб)", "Эл-во (руб)",
        "Содержание (руб)", "Наем (руб)", "ТКО (руб)", "Отопление+ОДН (руб)",
        "ИТОГО (руб)"
    ]
    ws.append(headers)

    total_sum = 0.0

    for user, r in result:
        row = [
            user.dormitory,
            user.username,
            user.apartment_area,
            f"{user.residents_count} / {user.total_room_residents}",
            r.cost_hot_water,
            r.cost_cold_water,
            r.cost_sewage,
            r.cost_electricity,
            r.cost_maintenance,
            r.cost_social_rent,
            r.cost_waste,
            r.cost_fixed_part,
            r.total_cost
        ]
        ws.append(row)
        total_sum += r.total_cost

    # Итоговая строка
    ws.append([""] * 11 + ["ИТОГО:", total_sum])

    # Сохраняем в память
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"Report_{period_name}.xlsx".replace(" ", "_")
    return output, filename