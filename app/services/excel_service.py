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
    try:
        wb = load_workbook(filename=io.BytesIO(file_content), read_only=True)
        ws = wb.active

        added_count = 0
        updated_count = 0
        skipped_count = 0
        errors = []

        # Получаем ВСЕХ существующих пользователей заранее
        existing_users_result = await db.execute(select(User.username))
        existing_usernames = {row[0] for row in existing_users_result.all()}

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
            # 6: Место работы (НОВОЕ)

            if not row or not row[0]:
                skipped_count += 1
                continue

            username = str(row[0]).strip()

            # Пропускаем пустые имена пользователей
            if not username:
                skipped_count += 1
                continue

            # Проверяем существование пользователя (используем предварительно полученный список)
            if username in existing_usernames:
                # Обновляем существующего пользователя
                try:
                    existing_user_result = await db.execute(
                        select(User).where(User.username == username)
                    )
                    existing_user = existing_user_result.scalars().first()

                    if existing_user:
                        # Обновляем поля пользователя
                        password = str(row[1]).strip() if (len(row) > 1 and row[1]) else None
                        dormitory = str(row[2]).strip() if (len(row) > 2 and row[2]) else existing_user.dormitory

                        try:
                            area = float(row[3]) if (len(row) > 3 and row[3]) else existing_user.apartment_area
                            residents = int(row[4]) if (len(row) > 4 and row[4]) else existing_user.residents_count
                            total_residents = int(row[5]) if (
                                        len(row) > 5 and row[5]) else existing_user.total_room_residents
                        except (ValueError, TypeError):
                            area = existing_user.apartment_area
                            residents = existing_user.residents_count
                            total_residents = existing_user.total_room_residents

                        workplace = str(row[6]).strip() if (len(row) > 6 and row[6]) else existing_user.workplace

                        # Обновляем данные пользователя
                        existing_user.dormitory = dormitory
                        existing_user.apartment_area = area
                        existing_user.residents_count = residents
                        existing_user.total_room_residents = total_residents
                        existing_user.workplace = workplace

                        # Обновляем пароль только если он указан
                        if password and password != username:
                            existing_user.hashed_password = get_password_hash(password)

                        updated_count += 1
                        errors.append(f"Строка {idx}: Обновлен пользователь '{username}'")
                    else:
                        # Это не должно случиться, но на всякий случай
                        skipped_count += 1
                        errors.append(f"Строка {idx}: Пользователь '{username}' не найден для обновления")

                except Exception as e:
                    errors.append(f"Строка {idx}: Ошибка обновления пользователя '{username}': {str(e)}")
                    skipped_count += 1

                continue  # Переходим к следующему пользователю

            # Создаем нового пользователя
            try:
                password = str(row[1]).strip() if (len(row) > 1 and row[1]) else username
                dormitory = str(row[2]).strip() if (len(row) > 2 and row[2]) else None

                try:
                    area = float(row[3]) if (len(row) > 3 and row[3]) else 0.0
                    residents = int(row[4]) if (len(row) > 4 and row[4]) else 1
                    total_residents = int(row[5]) if (len(row) > 5 and row[5]) else residents
                except (ValueError, TypeError):
                    errors.append(f"Строка {idx}: Ошибка числовых данных, используются значения по умолчанию")
                    area = 0.0
                    residents = 1
                    total_residents = residents

                # Читаем новое поле "Место работы"
                workplace = str(row[6]).strip() if (len(row) > 6 and row[6]) else None

                new_user = User(
                    username=username,
                    hashed_password=get_password_hash(password),
                    role="user",
                    dormitory=dormitory,
                    apartment_area=area,
                    residents_count=residents,
                    total_room_residents=total_residents,
                    workplace=workplace
                )
                db.add(new_user)
                added_count += 1

                # Добавляем в множество для последующих проверок
                existing_usernames.add(username)

            except Exception as e:
                errors.append(f"Строка {idx}: Ошибка создания пользователя '{username}': {str(e)}")
                skipped_count += 1
                continue

        # Коммитим все изменения одним разом
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            return {
                "status": "error",
                "message": f"Ошибка при сохранении в базу данных: {str(e)}",
                "added": 0,
                "updated": 0,
                "skipped": skipped_count,
                "errors": [f"Ошибка коммита: {str(e)}"]
            }

        return {
            "status": "success",
            "message": f"Успешно обработано {added_count + updated_count} записей",
            "added": added_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "errors": errors
        }

    except Exception as e:
        # Откатываем транзакцию в случае общей ошибки
        try:
            await db.rollback()
        except:
            pass

        return {
            "status": "error",
            "message": f"Ошибка обработки файла: {str(e)}",
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [str(e)]
        }


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