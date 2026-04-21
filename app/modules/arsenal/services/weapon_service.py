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
    async def process_document(
        db: AsyncSession, doc_data, items_data,
        attached_file_path: str = None, author_id: int = None,
        reverses_document_id: int = None,
    ):
        """
        Главный метод проводки документа.
        Атомарно создает документ и обновляет положение имущества в Реестре.

        Параметры:
            author_id — id ArsenalUser, который провёл документ (для аудита).
            reverses_document_id — если передан, создаётся «reversal» документа
                (внутри rollback_document). Исходник помечается is_reversed=True.
        """
        # Списание / Утилизация — обязательно с причиной.
        # Raw-создание без причины больше не допускается: иначе невозможно
        # ответить на вопрос «почему списано». Для reversal — не требуем
        # (это системная операция).
        if (
            doc_data.operation_type in ("Списание", "Утилизация")
            and not getattr(doc_data, "disposal_reason_id", None)
            and not reverses_document_id
        ):
            raise HTTPException(
                400,
                "Для операции «Списание»/«Утилизация» обязательна причина "
                "(disposal_reason_id). Справочник: GET /api/arsenal/disposal-reasons.",
            )

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
            operation_date=doc_data.operation_date,
            attached_file_path=attached_file_path,
            author_id=author_id,
            reverses_document_id=reverses_document_id,
            disposal_reason_id=getattr(doc_data, "disposal_reason_id", None),
            comment=getattr(doc_data, "comment", None),
        )
        db.add(new_doc)
        await db.flush()  # Получаем ID документа, чтобы привязывать строки

        # Если это reversal — отмечаем исходный документ.
        if reverses_document_id:
            orig = await db.get(Document, reverses_document_id)
            if orig and not orig.is_reversed:
                orig.is_reversed = True
                orig.reversed_by_document_id = new_doc.id
                db.add(orig)

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

        # Аудит: запись о проведении документа попадает в ту же транзакцию,
        # что и сам документ. Если проведение упадёт — лог тоже откатится.
        if author_id:
            from app.modules.arsenal.models import ArsenalUser
            from app.modules.arsenal.services.audit import write_arsenal_audit
            user = await db.get(ArsenalUser, author_id)
            action = "rollback_document" if reverses_document_id else "create_document"
            await write_arsenal_audit(
                db,
                user_id=author_id,
                username=user.username if user else "unknown",
                action=action,
                entity_type="document",
                entity_id=new_doc.id,
                details={
                    "doc_number": new_doc.doc_number,
                    "operation_type": new_doc.operation_type,
                    "source_id": new_doc.source_id,
                    "target_id": new_doc.target_id,
                    "items_count": len(items_data),
                    "reverses_document_id": reverses_document_id,
                },
            )

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

    # =========================================================================
    # ROLLBACK — создание обратного документа
    # =========================================================================
    @staticmethod
    async def rollback_document(
        db: AsyncSession, document_id: int, author_id: int, reason: str = None
    ):
        """Отменяет ранее проведённый документ созданием reversal-документа.

        Принцип: исходный документ остаётся в истории (военный учёт требует
        аудируемости). Создаётся новый документ с инвертированной логикой:
          * Перемещение A → B  ⇒  B → A
          * Отправка / Прием  ⇒  обратная пара
          * Первичный ввод    ⇒  Списание той же позиции
          * Списание          ⇒  Первичный ввод (возврат на source)

        После reversal исходный документ помечается is_reversed=True.
        """
        from sqlalchemy.orm import selectinload

        orig = (await db.execute(
            select(Document)
            .options(
                selectinload(Document.items).selectinload(DocumentItem.nomenclature),
                selectinload(Document.items).selectinload(DocumentItem.weapon),
            )
            .where(Document.id == document_id)
        )).scalars().first()

        if not orig:
            raise HTTPException(404, "Документ не найден")
        if orig.is_reversed:
            raise HTTPException(409, "Документ уже отменён (reversal существует)")
        if orig.reverses_document_id is not None:
            raise HTTPException(
                400,
                "Нельзя отменять reversal-документ. "
                "Для корректировки создайте прямой документ вручную.",
            )

        # Инвертируем операцию
        inverse_map = {
            "Первичный ввод": "Списание",
            "Списание":       "Первичный ввод",
            "Перемещение":    "Перемещение",
            "Отправка":       "Прием",
            "Прием":          "Отправка",
            "Утилизация":     "Первичный ввод",
        }
        new_op = inverse_map.get(orig.operation_type)
        if not new_op:
            raise HTTPException(
                400,
                f"Отмена операции '{orig.operation_type}' не поддерживается.",
            )

        # Для reversal меняем местами source и target (кроме first-input/disposal).
        if orig.operation_type == "Первичный ввод":
            rev_source_id, rev_target_id = orig.target_id, None  # списать оттуда, куда ввели
        elif orig.operation_type in ("Списание", "Утилизация"):
            rev_source_id, rev_target_id = None, orig.source_id   # вернуть на исходный склад
        else:
            rev_source_id, rev_target_id = orig.target_id, orig.source_id

        # Конструируем «псевдо-doc_data» для process_document
        class _RevDoc:
            doc_number = "АВТО"
            operation_type = new_op
            source_id = rev_source_id
            target_id = rev_target_id
            operation_date = datetime.utcnow()
            comment = f"Отмена документа #{orig.doc_number}" + (f": {reason}" if reason else "")
            disposal_reason_id = None

        # Строим items: копируем из оригинала. Для номерного учёта восстанавливаем
        # тот же серийник, для партионного — те же qty/nomenclature.
        class _RevItem:
            def __init__(self, src):
                self.nomenclature_id = src.nomenclature_id
                self.serial_number = src.serial_number
                self.quantity = src.quantity or 1
                self.price = float(src.price) if src.price is not None else None
                self.inventory_number = src.inventory_number

        rev_items = [_RevItem(i) for i in orig.items]
        return await WeaponService.process_document(
            db, _RevDoc, rev_items,
            attached_file_path=None,
            author_id=author_id,
            reverses_document_id=orig.id,
        )
