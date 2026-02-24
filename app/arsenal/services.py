# app/arsenal/services.py
import random
import string
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from app.arsenal.models import Document, DocumentItem, WeaponRegistry, Nomenclature


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
        # Если номер не пришел с фронта или равен "АВТО"
        if not doc_data.doc_number or doc_data.doc_number == "АВТО":
            for _ in range(10): # 10 попыток на случай коллизии (крайне маловероятно)
                new_num = WeaponService._generate_doc_number()
                # Проверяем, есть ли такой в базе
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
            # Получаем номенклатуру, чтобы понять стратегию учета (Номерной или Партионный)
            nomenclature = await db.get(Nomenclature, item.nomenclature_id)
            if not nomenclature:
                raise HTTPException(400, f"Номенклатура ID={item.nomenclature_id} не найдена")

            weapon_reg_id = None  # Ссылка на реестр (актуально в основном для номерного учета)

            if nomenclature.is_numbered:
                # === СТРАТЕГИЯ 1: НОМЕРНОЙ УЧЕТ (Автоматы, приборы) ===
                # Всегда 1 штука, строгий контроль уникальности серийника
                weapon_reg_id = await WeaponService._process_numbered(
                    db, doc_data, item, nomenclature
                )
            else:
                # === СТРАТЕГИЯ 2: ПАРТИОННЫЙ УЧЕТ (Патроны, расходники) ===
                # Работаем с количествами, списываем из одной кучи, добавляем в другую
                await WeaponService._process_batch(
                    db, doc_data, item, nomenclature
                )

            # 3. Записываем строку в историю (состав документа)
            doc_item = DocumentItem(
                document_id=new_doc.id,
                weapon_id=weapon_reg_id,
                nomenclature_id=item.nomenclature_id,
                serial_number=item.serial_number,
                quantity=item.quantity
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
        # Ищем глобально активное оружие с таким номером
        stmt = select(WeaponRegistry).where(
            WeaponRegistry.nomenclature_id == item.nomenclature_id,
            WeaponRegistry.serial_number == serial,
            WeaponRegistry.status == 1  # Только активное
        )
        existing_weapon = (await db.execute(stmt)).scalars().first()

        # 2. Логика по типу операции
        op_type = doc_data.operation_type

        if op_type == "Первичный ввод":
            if existing_weapon:
                raise HTTPException(
                    400,
                    f"Ошибка ввода: Изделие {nomenclature.name} №{serial} уже стоит на учете (ID объекта: {existing_weapon.current_object_id})"
                )

            # Создаем новую карточку
            weapon = WeaponRegistry(
                nomenclature_id=item.nomenclature_id,
                serial_number=serial,
                current_object_id=doc_data.target_id,
                status=1,
                quantity=1
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
                # Деактивируем
                weapon.current_object_id = None
                weapon.status = 0
            else:
                # Перемещаем
                weapon.current_object_id = doc_data.target_id

            db.add(weapon)

        else:
            raise HTTPException(400, f"Неизвестный тип операции: {op_type}")

        return weapon.id if weapon else None

    # =========================================================================
    # ЛОГИКА ПАРТИОННОГО УЧЕТА (is_numbered = False)
    # =========================================================================
    @staticmethod
    async def _process_batch(db: AsyncSession, doc_data, item, nomenclature):
        """
        Обработка перемещения количества (партии).
        item.serial_number здесь выступает как Номер партии (или год).
        item.quantity критически важно.
        """
        batch_number = item.serial_number
        qty = item.quantity
        op_type = doc_data.operation_type

        # ---------------------------------------------------
        # ШАГ 1: СПИСАНИЕ (УМЕНЬШЕНИЕ) У ОТПРАВИТЕЛЯ
        # (Актуально для Списания, Перемещения, Отправки)
        # ---------------------------------------------------
        if op_type != "Первичный ввод" and doc_data.source_id:
            # Ищем партию на складе отправителя
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

            # Уменьшаем остаток
            source_reg.quantity -= qty

            # Если остаток 0 — удаляем запись, чтобы не засорять базу
            if source_reg.quantity == 0:
                await db.delete(source_reg)
            else:
                db.add(source_reg)

        # ---------------------------------------------------
        # ШАГ 2: ЗАЧИСЛЕНИЕ (УВЕЛИЧЕНИЕ) ПОЛУЧАТЕЛЮ
        # (Актуально для Ввода, Перемещения, Приема)
        # ---------------------------------------------------
        if op_type != "Списание" and doc_data.target_id:
            # Ищем, есть ли уже такая партия у получателя
            stmt_target = select(WeaponRegistry).where(
                WeaponRegistry.nomenclature_id == item.nomenclature_id,
                WeaponRegistry.serial_number == batch_number,
                WeaponRegistry.current_object_id == doc_data.target_id,
                WeaponRegistry.status == 1
            )
            target_reg = (await db.execute(stmt_target)).scalars().first()

            if target_reg:
                # Если партия есть, просто плюсуем
                target_reg.quantity += qty
                db.add(target_reg)
            else:
                # Если партии нет, создаем новую запись
                new_reg = WeaponRegistry(
                    nomenclature_id=item.nomenclature_id,
                    serial_number=batch_number,
                    current_object_id=doc_data.target_id,
                    status=1,
                    quantity=qty
                )
                db.add(new_reg)