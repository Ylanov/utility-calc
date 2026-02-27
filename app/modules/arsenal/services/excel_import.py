import io
import re
import logging
from typing import Dict, List
from decimal import Decimal
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.modules.arsenal.models import AccountingObject, Nomenclature, WeaponRegistry

# Настройка логгера
logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


def parse_price(value) -> Decimal:
    """Очищает строку с ценой от пробелов и приводит к Decimal"""
    if value is None:
        return ZERO
    # Если уже число
    if isinstance(value, (int, float)):
        return Decimal(f"{value:.2f}")
    if isinstance(value, Decimal):
        return value

    # Если строка - чистим мусор
    clean_val = str(value).replace(' ', '').replace(',', '.').replace('\xa0', '').strip()
    try:
        return Decimal(clean_val)
    except:
        return ZERO


async def import_arsenal_from_excel(file_content: bytes, db: AsyncSession) -> dict:
    try:
        logger.info("🟢 Начинаю обработку Excel файла Арсенала...")

        # Загружаем Excel
        workbook = load_workbook(filename=io.BytesIO(file_content), read_only=True, data_only=True)
        worksheet = workbook.active

        added_count = 0
        updated_count = 0
        skipped_count = 0
        errors: List[str] = []

        # === КЭШИРОВАНИЕ ===
        objects_cache: Dict[str, AccountingObject] = {}
        nom_cache: Dict[str, Nomenclature] = {}

        # Кэш складов
        obj_res = await db.execute(select(AccountingObject))
        for obj in obj_res.scalars().all():
            key = f"{obj.name.lower()}_{str(obj.mol_name).lower() if obj.mol_name else ''}"
            objects_cache[key] = obj

        # Кэш номенклатуры
        nom_res = await db.execute(select(Nomenclature))
        for nom in nom_res.scalars().all():
            nom_cache[nom.name.lower()] = nom

        # Читаем строки
        rows = worksheet.iter_rows(values_only=True)

        for row_index, row in enumerate(rows, start=1):
            try:
                # 1. Проверка на валидность строки (должна начинаться с номера п/п)
                if not row or row[0] is None:
                    continue

                # Очищаем первую ячейку от точек и пробелов (чтобы "1." стало "1")
                first_col = str(row[0]).strip().replace('.', '')

                if not first_col.isdigit():
                    if skipped_count < 5:
                        logger.warning(f"⚠️ Пропуск строки {row_index}: Первая колонка '{row[0]}' не является числом.")
                    skipped_count += 1
                    continue

                # 2. Чтение данных (СТРОГО ПО ИНДЕКСАМ ИЗ ВАШЕГО EXCEL)
                # A[0] - №
                # B[1] - Наименование
                # C[2] - Счет
                # D[3] - КБК (ПРОПУСКАЕМ)
                # E[4] - Место хранения
                # F[5] - Количество
                # G[6] - Сумма
                # H[7] - Инв. номер

                raw_name = str(row[1]).strip() if len(row) > 1 and row[1] else ""
                if not raw_name:
                    skipped_count += 1
                    continue

                account = str(row[2]).strip() if len(row) > 2 and row[2] else None

                # E (индекс 4) - Место хранения
                storage_raw = str(row[4]).strip() if len(row) > 4 and row[4] else "Главный склад"

                # F (индекс 5) - Количество
                qty_raw = row[5] if len(row) > 5 else 1
                try:
                    if isinstance(qty_raw, str):
                        qty_raw = qty_raw.replace(' ', '').replace('\xa0', '')
                    qty = int(float(qty_raw)) if qty_raw else 1
                except:
                    # Если не удалось прочитать количество, ставим 1
                    qty = 1

                # G (индекс 6) - Цена
                price = parse_price(row[6] if len(row) > 6 else 0)

                # H (индекс 7) - Инвентарный номер
                inv_number = str(row[7]).strip() if len(row) > 7 and row[7] else None

                # 3. Парсинг Склада и МОЛ
                obj_name = storage_raw
                mol_name = None

                if " - " in storage_raw:
                    parts = storage_raw.rsplit(" - ", 1)
                    obj_name = parts[0].strip()
                    mol_name = parts[1].strip()

                # Получаем или создаем склад
                obj_cache_key = f"{obj_name.lower()}_{str(mol_name).lower() if mol_name else ''}"
                target_object = objects_cache.get(obj_cache_key)

                if not target_object:
                    target_object = AccountingObject(name=obj_name, obj_type="Склад", mol_name=mol_name)
                    db.add(target_object)
                    await db.flush()
                    objects_cache[obj_cache_key] = target_object

                # 4. Парсинг Номенклатуры
                serial_number = "Б/Н"
                nom_name = raw_name

                match = re.search(r'\(([^)]+)\)$', raw_name)
                if match:
                    serial_number = match.group(1).strip()
                    nom_name = raw_name[:match.start()].strip()

                is_numbered = True
                if account and str(account).strip().startswith("105."):
                    is_numbered = False
                    if serial_number == "Б/Н":
                        serial_number = "Партия 1"

                nom_cache_key = nom_name.lower()
                nomenclature = nom_cache.get(nom_cache_key)

                if not nomenclature:
                    nomenclature = Nomenclature(
                        name=nom_name,
                        default_account=account,
                        is_numbered=is_numbered
                    )
                    db.add(nomenclature)
                    await db.flush()
                    nom_cache[nom_cache_key] = nomenclature

                # 5. Обновляем остатки
                stmt = select(WeaponRegistry).where(
                    WeaponRegistry.nomenclature_id == nomenclature.id,
                    WeaponRegistry.serial_number == serial_number,
                    WeaponRegistry.current_object_id == target_object.id,
                    WeaponRegistry.status == 1
                )
                existing_weapon = (await db.execute(stmt)).scalars().first()

                if existing_weapon:
                    if not is_numbered:
                        existing_weapon.quantity += qty
                        existing_weapon.price = (existing_weapon.price or ZERO) + price
                    else:
                        existing_weapon.price = price
                        existing_weapon.inventory_number = inv_number
                    updated_count += 1
                else:
                    new_weapon = WeaponRegistry(
                        nomenclature_id=nomenclature.id,
                        serial_number=serial_number,
                        current_object_id=target_object.id,
                        status=1,
                        quantity=qty,
                        inventory_number=inv_number,
                        price=price,
                        account_code=account
                    )
                    db.add(new_weapon)
                    added_count += 1

            except Exception as row_error:
                skipped_count += 1
                err_msg = f"Строка {row_index}: {str(row_error)}"
                errors.append(err_msg)
                logger.error(err_msg)

        await db.commit()
        workbook.close()

        logger.info(f"🏁 Импорт завершен. +{added_count}, ~{updated_count}, -{skipped_count}")

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
        logger.exception("Критическая ошибка импорта")
        return {
            "status": "error",
            "message": f"Ошибка: {str(error)}",
            "added": 0,
            "updated": 0,
            "skipped": 0,
            "errors": [str(error)]
        }