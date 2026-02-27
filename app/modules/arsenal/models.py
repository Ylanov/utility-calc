from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    DateTime,
    Boolean,
    Text,
    UniqueConstraint,
    Numeric  # НОВОЕ: Импортируем Numeric для работы с деньгами/ценами
)
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import ArsenalBase


# --- Пользователи Арсенала ---
class ArsenalUser(ArsenalBase):
    __tablename__ = "arsenal_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)

    # Роль пользователя (admin - видит всё, unit_head - видит только свое)
    role = Column(String, default="unit_head")

    # Привязка к конкретному складу/подразделению
    object_id = Column(Integer, ForeignKey("accounting_objects.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Связь
    accounting_object = relationship("AccountingObject")


# --- Справочник: Организации / Объекты учета ---
class AccountingObject(ArsenalBase):
    __tablename__ = "accounting_objects"

    id = Column(Integer, primary_key=True, index=True)
    parent_id = Column(Integer, ForeignKey("accounting_objects.id"), nullable=True)
    name = Column(String, nullable=False)

    # Тип объекта: Подразделение, Склад, Ремонт, Контрагент
    obj_type = Column(String, nullable=False)

    # 🔥 НОВОЕ: Материально-ответственное лицо (МОЛ), например: "Матус А. А."
    mol_name = Column(String, nullable=True)

    # Иерархия объектов (Self-referential relationship)
    children = relationship("AccountingObject", backref="parent", remote_side=[id])


# --- Справочник: Номенклатура (Изделия) ---
class Nomenclature(ArsenalBase):
    __tablename__ = "nomenclature"

    id = Column(Integer, primary_key=True, index=True)

    # Индекс ГРАУ (например: 6П20)
    code = Column(String, index=True)

    name = Column(String, nullable=False)
    category = Column(String, nullable=True)

    # 🔥 НОВОЕ: Счет учета по умолчанию (например: 101.34.1, 105.36.1)
    default_account = Column(String, nullable=True)

    # ФЛАГ ТИПА УЧЕТА
    # True  = Номерной (Автоматы). quantity всегда 1. serial_number уникален глобально.
    # False = Партионный (Патроны). quantity > 0. serial_number = Номер партии.
    is_numbered = Column(Boolean, default=True, nullable=False)


# --- ГЛАВНАЯ ТАБЛИЦА: РЕЕСТР ОРУЖИЯ И БОЕПРИПАСОВ (КАРТОТЕКА) ---
class WeaponRegistry(ArsenalBase):
    __tablename__ = "weapon_registry"

    id = Column(Integer, primary_key=True, index=True)

    nomenclature_id = Column(Integer, ForeignKey("nomenclature.id"), nullable=False)

    # Если is_numbered=True -> Серийный/Заводской номер изделия
    # Если is_numbered=False -> Номер партии (или год, если партии нет)
    serial_number = Column(String, nullable=False, index=True)

    year_of_manufacture = Column(Integer, nullable=True)

    # Текущее местонахождение
    current_object_id = Column(Integer, ForeignKey("accounting_objects.id"), nullable=True)

    # Статус:
    # 1 - В наличии
    # 0 - Списано / Уничтожено
    # 2 - В ремонте
    status = Column(Integer, default=1)

    # КОЛИЧЕСТВО
    # Для номерного учета всегда 1.
    # Для партионного учета здесь хранится остаток партии на данном объекте.
    quantity = Column(Integer, default=1)

    # 🔥 НОВЫЕ БУХГАЛТЕРСКИЕ ПОЛЯ (из вашей выгрузки TXT):
    # Инвентарный номер (например: 1101341304594)
    inventory_number = Column(String, index=True, nullable=True)

    # Цена / Сумма (Numeric(15,2) позволяет хранить суммы до десятков миллиардов с копейками)
    price = Column(Numeric(15, 2), nullable=True)

    # Фактический счет учета для конкретной единицы (если отличается от дефолтного в номенклатуре)
    account_code = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Связи
    nomenclature = relationship("Nomenclature")
    current_object = relationship("AccountingObject")

    # Уникальность:
    # Теперь уникальна связка: (Изделие + Партия/Номер + Местонахождение).
    # Это позволяет хранить одну и ту же партию патронов на разных складах разными строками.
    __table_args__ = (
        UniqueConstraint(
            "nomenclature_id",
            "serial_number",
            "current_object_id",
            name="uix_nom_serial_obj"
        ),
    )


# --- Документы ---
class Document(ArsenalBase):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    doc_number = Column(String, index=True)

    doc_date = Column(DateTime, default=datetime.utcnow)
    operation_date = Column(DateTime, default=datetime.utcnow)

    # Тип операции:
    # 'Первичный ввод' (INCOME)
    # 'Перемещение' / 'Выдача' / 'Прием' (TRANSFER)
    # 'Списание' (OUTCOME)
    operation_type = Column(String, nullable=False)

    source_id = Column(Integer, ForeignKey("accounting_objects.id"), nullable=True)
    target_id = Column(Integer, ForeignKey("accounting_objects.id"), nullable=True)

    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    author_id = Column(Integer, ForeignKey("arsenal_users.id"), nullable=True)

    # Связи
    source = relationship("AccountingObject", foreign_keys=[source_id])
    target = relationship("AccountingObject", foreign_keys=[target_id])
    items = relationship(
        "DocumentItem",
        back_populates="document",
        cascade="all, delete"
    )


# --- Состав документа (Строки накладной) ---
class DocumentItem(ArsenalBase):
    __tablename__ = "document_items"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(Integer, ForeignKey("documents.id"))

    # Ссылка на конкретную запись реестра (может быть NULL, если запись была удалена при списании в ноль)
    weapon_id = Column(Integer, ForeignKey("weapon_registry.id"), nullable=True)

    # Дублирование данных для истории (Snapshot)
    nomenclature_id = Column(Integer, ForeignKey("nomenclature.id"))

    # Серийный номер ИЛИ Номер партии
    serial_number = Column(String, nullable=True)

    # 🔥 НОВОЕ: Дублирование бухгалтерских данных в историю (на момент совершения операции)
    inventory_number = Column(String, nullable=True)
    price = Column(Numeric(15, 2), nullable=True)

    # Количество в этой операции
    quantity = Column(Integer, default=1)

    document = relationship("Document", back_populates="items")
    nomenclature = relationship("Nomenclature")
    weapon = relationship("WeaponRegistry")