import random
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi import HTTPException
from app.modules.gsm.models import GsmDocument, GsmDocumentItem, FuelRegistry, GsmNomenclature


class GsmService:
    @staticmethod
    def _generate_doc_number():
        """
        Генерирует номер формата GSM-YYXXXX (например GSM-26A1F).
        YY - год (26)
        XXXX - случайные буквы и цифры
        """
        year = datetime.now().strftime("%y")
        # Исключаем буквы I, O, чтобы не путать с 1 и 0
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789"
        suffix = ''.join(random.choices(chars, k=4))
        return f"GSM-{year}{suffix}"

    @staticmethod
    async def process_document(db: AsyncSession, doc_data, items_data):
        """
        Главный метод проводки документа движения ГСМ.
        Атомарно создает документ и обновляет остатки в резервуарах/на складах.
        """
        # --- ГЕНЕРАЦИЯ НОМЕРА (АВТОМАТИЧЕСКИ) ---
        if not doc_data.doc_number or doc_data.doc_number == "АВТО":
            for _ in range(10): # 10 попыток на случай коллизии
                new_num = GsmService._generate_doc_number()
                exists = await db.execute(select(GsmDocument).where(GsmDocument.doc_number == new_num))
                if not exists.scalars().first():
                    doc_data.doc_number = new_num
                    break
            else:
                raise HTTPException(500, "Не удалось сгенерировать уникальный номер документа ГСМ.")

        # 1. Создаем шапку документа
        new_doc = GsmDocument(
            doc_number=doc_data.doc_number,
            operation_type=doc_data.operation_type,
            source_id=doc_data.source_id,
            target_id=doc_data.target_id,
            operation_date=doc_data.operation_date
        )
        db.add(new_doc)
        await db.flush()  # Получаем ID документа

        # 2. Обрабатываем каждую строку спецификации
        for item in items_data:
            # Получаем номенклатуру, чтобы понять стратегию учета (Фасовка или Налив)
            nomenclature = await db.get(GsmNomenclature, item.nomenclature_id)
            if not nomenclature:
                raise HTTPException(400, f"Марка ГСМ ID={item.nomenclature_id} не найдена")

            fuel_reg_id = None  # Ссылка на конкретную запись в реестре

            if nomenclature.is_packaged:
                # === СТРАТЕГИЯ 1: ФАСОВАННАЯ ПРОДУКЦИЯ (Бочки, канистры) ===
                # Всегда 1 штука, строгий контроль уникальности штрихкода тары
                fuel_reg_id = await GsmService._process_packaged(
                    db, doc_data, item, nomenclature
                )
            else:
                # === СТРАТЕГИЯ 2: НАЛИВНОЙ УЧЕТ (Топливо в резервуарах) ===
                # Работаем с объемами (дроби), списываем литры, добавляем литры по Паспорту
                await GsmService._process_bulk(
                    db, doc_data, item, nomenclature
                )

            # 3. Записываем строку в историю (состав документа)
            # В схеме фронтенда поле называется serial_number, в БД ГСМ это batch_number
            doc_item = GsmDocumentItem(
                document_id=new_doc.id,
                fuel_id=fuel_reg_id,
                nomenclature_id=item.nomenclature_id,
                batch_number=item.serial_number,
                quantity=item.quantity
            )
            db.add(doc_item)

        # Фиксируем транзакцию
        await db.commit()
        await db.refresh(new_doc)
        return new_doc

    # =========================================================================
    # ЛОГИКА ФАСОВАННОГО УЧЕТА (is_packaged = True)
    # =========================================================================
    @staticmethod
    async def _process_packaged(db: AsyncSession, doc_data, item, nomenclature):
        """
        Обработка перемещения конкретной тары (бочки).
        item.quantity игнорируется (всегда считается за 1 шт).
        В item.serial_number лежит штрихкод или номер бочки.
        """
        barcode = item.serial_number
        fuel_item = None

        # 1. Поиск существующей бочки
        stmt = select(FuelRegistry).where(
            FuelRegistry.nomenclature_id == item.nomenclature_id,
            FuelRegistry.batch_number == barcode,
            FuelRegistry.status == 1  # Только активные
        )
        existing_container = (await db.execute(stmt)).scalars().first()

        op_type = doc_data.operation_type

        if op_type == "Первичный ввод":
            if existing_container:
                raise HTTPException(
                    400,
                    f"Ошибка: Тара {nomenclature.name} №{barcode} уже числится на объекте ID={existing_container.current_object_id}"
                )

            # Создаем новую запись для бочки
            fuel_item = FuelRegistry(
                nomenclature_id=item.nomenclature_id,
                batch_number=barcode,
                current_object_id=doc_data.target_id,
                status=1,
                quantity=1  # 1 штука
            )
            db.add(fuel_item)
            await db.flush()

        elif op_type in ["Перемещение", "Отправка", "Прием", "Списание"]:
            if not existing_container:
                raise HTTPException(400, f"Ошибка: Тара {nomenclature.name} №{barcode} не найдена на балансе.")

            fuel_item = existing_container

            # Проверка владельца
            if doc_data.source_id and fuel_item.current_object_id != doc_data.source_id:
                raise HTTPException(
                    400,
                    f"Ошибка: Тара №{barcode} числится не здесь, а на объекте ID={fuel_item.current_object_id}"
                )

            if op_type == "Списание":
                # Деактивируем (выдали/израсходовали)
                fuel_item.current_object_id = None
                fuel_item.status = 0
            else:
                # Перемещаем на другой склад
                fuel_item.current_object_id = doc_data.target_id

            db.add(fuel_item)

        else:
            raise HTTPException(400, f"Неизвестный тип операции: {op_type}")

        return fuel_item.id if fuel_item else None

    # =========================================================================
    # ЛОГИКА НАЛИВНОГО/ОБЪЕМНОГО УЧЕТА (is_packaged = False)
    # =========================================================================
    @staticmethod
    async def _process_bulk(db: AsyncSession, doc_data, item, nomenclature):
        """
        Обработка перемещения объемов (налив).
        В item.serial_number лежит Паспорт качества или Номер партии.
        item.quantity содержит объем (например, 1500.50 л).
        """
        batch_number = item.serial_number
        qty = item.quantity
        op_type = doc_data.operation_type

        # ---------------------------------------------------
        # ШАГ 1: СПИСАНИЕ (УМЕНЬШЕНИЕ) ИЗ РЕЗЕРВУАРА ОТПРАВИТЕЛЯ
        # ---------------------------------------------------
        if op_type != "Первичный ввод" and doc_data.source_id:
            stmt_source = select(FuelRegistry).where(
                FuelRegistry.nomenclature_id == item.nomenclature_id,
                FuelRegistry.batch_number == batch_number,
                FuelRegistry.current_object_id == doc_data.source_id,
                FuelRegistry.status == 1
            )
            source_reg = (await db.execute(stmt_source)).scalars().first()

            if not source_reg:
                raise HTTPException(
                    400,
                    f"Паспорт/Партия '{batch_number}' ({nomenclature.name}) не найдена в резервуаре отправителя."
                )

            if source_reg.quantity < qty:
                raise HTTPException(
                    400,
                    f"Недостаточно объема по паспорту '{batch_number}'. В наличии: {source_reg.quantity}, Требуется: {qty}"
                )

            # Уменьшаем объем
            source_reg.quantity -= qty

            # Если объем стал 0 — удаляем запись (очищаем резервуар от этой партии)
            if source_reg.quantity == 0:
                await db.delete(source_reg)
            else:
                db.add(source_reg)

        # ---------------------------------------------------
        # ШАГ 2: ЗАЧИСЛЕНИЕ (УВЕЛИЧЕНИЕ) В РЕЗЕРВУАР ПОЛУЧАТЕЛЯ
        # ---------------------------------------------------
        if op_type != "Списание" and doc_data.target_id:
            stmt_target = select(FuelRegistry).where(
                FuelRegistry.nomenclature_id == item.nomenclature_id,
                FuelRegistry.batch_number == batch_number,
                FuelRegistry.current_object_id == doc_data.target_id,
                FuelRegistry.status == 1
            )
            target_reg = (await db.execute(stmt_target)).scalars().first()

            if target_reg:
                # Если топливо с таким же паспортом уже есть в резервуаре, просто плюсуем объем
                target_reg.quantity += qty
                db.add(target_reg)
            else:
                # Если это новый паспорт для этого резервуара, создаем запись
                new_reg = FuelRegistry(
                    nomenclature_id=item.nomenclature_id,
                    batch_number=batch_number,
                    current_object_id=doc_data.target_id,
                    status=1,
                    quantity=qty
                )
                db.add(new_reg)