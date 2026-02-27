import random
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from app.modules.arsenal.models import Document, DocumentItem, WeaponRegistry, Nomenclature


class WeaponService:
    @staticmethod
    def _generate_doc_number():
        """
        Генерирует номер формата YYXXXX (например 26A1F9).
        YY - год (26)
        XXXX - случайные буквы и цифры
        """
        year = datetime.now().strftime("%y")
        # Исключаем буквы I, O, чтобы не путать с 1 и 0
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
        suffix = ''.join(random.choices(chars, k=4))
        return f"{year}{suffix}"

    @staticmethod
    async def process_document(db: AsyncSession, doc_data, items_data):
        """
        Главный метод проводки документа.
        Атомарно создает документ и обновляет положение имущества в Реестре.
        """
        # --- ГЕНЕРАЦИЯ НОМЕРА (АВТОМАТИЧЕСКИ) ---
        if not doc_data.doc_number or doc_data.doc_number == "АВТО":
            for _ in range(10): # 10 попыток на случай коллизии
                new_num = WeaponService._generate_doc_number()
                exists = await db.execute(select(Document).where(Document.doc_number == new_num))
                if not exists.scalars().first():
                    doc_data.doc_number = new_num
                    break
            else:
                raise HTTPException(500, "Не удалось сгенерировать уникальный номер документа. Попробуйте еще раз.")

        # 1. Создаем шапку документа
        new_doc = Document(
            doc_number=doc_data.doc_number,
            operation_type=doc_data.operation_type,
            source_id=doc_data.source_id,
            target_id=doc_data.target_id,
            operation_date=doc_data.operation_date
        )
        db.add(new_doc)
        await db.flush()  # Получаем ID документа, чтобы привязывать строки

        # 2. Обрабатываем каждую строку спецификации
        for item in items_data:
            nomenclature = await db.get(Nomenclature, item.nomenclature_id)
            if not nomenclature:
                raise HTTPException(400, f"Номенклатура ID={item.nomenclature_id} не найдена")

            # В зависимости от типа учета, получаем обновленную/созданную запись из реестра
            if nomenclature.is_numbered:
                # НОМЕРНОЙ УЧЕТ
                weapon_reg = await WeaponService._process_numbered(
                    db, doc_data, item, nomenclature
                )
            else:
                # ПАРТИОННЫЙ УЧЕТ
                weapon_reg = await WeaponService._process_batch(
                    db, doc_data, item, nomenclature
                )

            # Определяем цену и инвентарный номер для записи в историю накладной.
            history_price = item.price if item.price is not None else (weapon_reg.price if weapon_reg else None)
            history_inv = item.inventory_number if item.inventory_number else (weapon_reg.inventory_number if weapon_reg else None)

            # Проверка: если партия списана в ноль, ее удалили из БД, ID привязывать нельзя (FK Constraint)
            weapon_id_to_link = weapon_reg.id if weapon_reg and hasattr(weapon_reg, 'id') else None

            # 3. Записываем строку в историю (состав документа)
            doc_item = DocumentItem(
                document_id=new_doc.id,
                weapon_id=weapon_id_to_link,
                nomenclature_id=item.nomenclature_id,
                serial_number=item.serial_number,
                quantity=item.quantity,
                inventory_number=history_inv, # НОВОЕ: Сохраняем инвентарник в истории
                price=history_price           # НОВОЕ: Сохраняем цену в истории
            )
            db.add(doc_item)

        # Фиксируем транзакцию
        await db.commit()
        await db.refresh(new_doc)
        return new_doc

    # =========================================================================
    # ЛОГИКА НОМЕРНОГО УЧЕТА (is_numbered = True)
    # =========================================================================
    @staticmethod
    async def _process_numbered(db: AsyncSession, doc_data, item, nomenclature):
        """
        Обработка перемещения конкретной единицы оружия.
        item.quantity игнорируется (всегда считается за 1).
        """
        serial = item.serial_number
        weapon = None

        # 1. Поиск существующей карточки (если есть)
        stmt = select(WeaponRegistry).where(
            WeaponRegistry.nomenclature_id == item.nomenclature_id,
            WeaponRegistry.serial_number == serial,
            WeaponRegistry.status == 1  # Только активное
        )
        existing_weapon = (await db.execute(stmt)).scalars().first()

        op_type = doc_data.operation_type

        if op_type == "Первичный ввод":
            if existing_weapon:
                raise HTTPException(
                    400,
                    f"Ошибка ввода: Изделие {nomenclature.name} №{serial} уже стоит на учете (ID объекта: {existing_weapon.current_object_id})"
                )

            # Создаем новую карточку со всеми бухгалтерскими данными
            weapon = WeaponRegistry(
                nomenclature_id=item.nomenclature_id,
                serial_number=serial,
                current_object_id=doc_data.target_id,
                status=1,
                quantity=1,
                inventory_number=item.inventory_number,  # НОВОЕ
                price=item.price                         # НОВОЕ
            )
            db.add(weapon)
            await db.flush()  # Чтобы получить ID

        elif op_type in ["Перемещение", "Отправка", "Прием", "Списание"]:
            if not existing_weapon:
                raise HTTPException(400, f"Ошибка: Изделие {nomenclature.name} №{serial} не найдено на балансе.")

            weapon = existing_weapon

            # Проверка владельца (действительно ли списываем откуда надо)
            if doc_data.source_id and weapon.current_object_id != doc_data.source_id:
                raise HTTPException(
                    400,
                    f"Ошибка: Изделие №{serial} числится не здесь, а на объекте ID={weapon.current_object_id}"
                )

            if op_type == "Списание":
                weapon.current_object_id = None
                weapon.status = 0
            else:
                weapon.current_object_id = doc_data.target_id

            db.add(weapon)

        else:
            raise HTTPException(400, f"Неизвестный тип операции: {op_type}")

        return weapon

    # =========================================================================
    # ЛОГИКА ПАРТИОННОГО УЧЕТА (is_numbered = False)
    # =========================================================================
    @staticmethod
    async def _process_batch(db: AsyncSession, doc_data, item, nomenclature):
        """
        Обработка перемещения количества (партии).
        """
        batch_number = item.serial_number
        qty = item.quantity
        op_type = doc_data.operation_type

        source_reg = None
        target_reg = None
        is_source_deleted = False

        # ---------------------------------------------------
        # ШАГ 1: СПИСАНИЕ (УМЕНЬШЕНИЕ) У ОТПРАВИТЕЛЯ
        # ---------------------------------------------------
        if op_type != "Первичный ввод" and doc_data.source_id:
            stmt_source = select(WeaponRegistry).where(
                WeaponRegistry.nomenclature_id == item.nomenclature_id,
                WeaponRegistry.serial_number == batch_number,
                WeaponRegistry.current_object_id == doc_data.source_id,
                WeaponRegistry.status == 1
            )
            source_reg = (await db.execute(stmt_source)).scalars().first()

            if not source_reg:
                raise HTTPException(
                    400,
                    f"Партия '{batch_number}' ({nomenclature.name}) не найдена у отправителя."
                )

            if source_reg.quantity < qty:
                raise HTTPException(
                    400,
                    f"Недостаточно остатка по партии '{batch_number}'. Есть: {source_reg.quantity}, Требуется: {qty}"
                )

            source_reg.quantity -= qty

            if source_reg.quantity == 0:
                await db.delete(source_reg)
                is_source_deleted = True
            else:
                db.add(source_reg)

        # ---------------------------------------------------
        # ШАГ 2: ЗАЧИСЛЕНИЕ (УВЕЛИЧЕНИЕ) ПОЛУЧАТЕЛЮ
        # ---------------------------------------------------
        if op_type != "Списание" and doc_data.target_id:
            stmt_target = select(WeaponRegistry).where(
                WeaponRegistry.nomenclature_id == item.nomenclature_id,
                WeaponRegistry.serial_number == batch_number,
                WeaponRegistry.current_object_id == doc_data.target_id,
                WeaponRegistry.status == 1
            )
            target_reg = (await db.execute(stmt_target)).scalars().first()

            if target_reg:
                target_reg.quantity += qty
                db.add(target_reg)
                return target_reg
            else:
                new_reg = WeaponRegistry(
                    nomenclature_id=item.nomenclature_id,
                    serial_number=batch_number,
                    current_object_id=doc_data.target_id,
                    status=1,
                    quantity=qty,
                    inventory_number=item.inventory_number,
                    price=item.price
                )
                db.add(new_reg)
                await db.flush()
                return new_reg

        if op_type == "Списание":
            if is_source_deleted:
                class DummyReg:
                    id = None
                    price = source_reg.price
                    inventory_number = source_reg.inventory_number
                return DummyReg()
            return source_reg

        return None