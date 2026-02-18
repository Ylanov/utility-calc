# app/arsenal/models.py
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Enum, Boolean, Text
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.database import ArsenalBase


# --- Энумерация типов операций (с Фото 1) ---
class OperationType(str, enum.Enum):
    INITIAL_ENTRY = "Первичный ввод"
    SHIPMENT = "Отправка"
    RECEIPT = "Прием"
    LOSS = "Утрата (хищение)"
    UTILIZATION = "Утилизация"
    WRITE_OFF = "Списание"
    TRANSFER = "Перемещение"
    CATEGORIZATION = "Категорирование"


# --- Пользователи Арсенала (без ролей, просто доступ) ---
class ArsenalUser(ArsenalBase):
    __tablename__ = "arsenal_users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)


# --- Справочник: Организации/Объекты учета (Фото 5, 6) ---
# Древовидная структура: Склад -> Подразделение
class AccountingObject(ArsenalBase):
    __tablename__ = "accounting_objects"
    id = Column(Integer, primary_key=True, index=True)
    parent_id = Column(Integer, ForeignKey("accounting_objects.id"), nullable=True)

    name = Column(String, nullable=False)  # Условное наименование (напр. "Рота связи")
    obj_type = Column(String, nullable=False)  # Тип: Склад, Подразделение, Внешний контрагент

    # Связь с родительским объектом (для дерева)
    children = relationship("AccountingObject", backref="parent", remote_side=[id])


# --- Справочник: Номенклатура (Изделия) (Фото 4) ---
class Nomenclature(ArsenalBase):
    __tablename__ = "nomenclature"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, index=True)  # Индекс ГРАУ или код
    name = Column(String, nullable=False)  # Наименование (напр. "АК-74М")
    category = Column(String, nullable=True)  # Категория


# --- Документы (Фото 1, 3, 7) ---
class Document(ArsenalBase):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, index=True)

    doc_number = Column(String, index=True)  # Номер документа
    doc_date = Column(DateTime, default=datetime.utcnow)  # Дата документа
    operation_date = Column(DateTime, default=datetime.utcnow)  # Дата операции

    operation_type = Column(Enum(OperationType), nullable=False)

    # Откуда и Куда (связь с объектами учета)
    source_id = Column(Integer, ForeignKey("accounting_objects.id"), nullable=True)
    target_id = Column(Integer, ForeignKey("accounting_objects.id"), nullable=True)

    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    author_id = Column(Integer, ForeignKey("arsenal_users.id"))

    # Связи
    source = relationship("AccountingObject", foreign_keys=[source_id])
    target = relationship("AccountingObject", foreign_keys=[target_id])
    items = relationship("DocumentItem", back_populates="document", cascade="all, delete")


# --- Состав документа (Строки с изделиями) ---
class DocumentItem(ArsenalBase):
    __tablename__ = "document_items"
    id = Column(Integer, primary_key=True, index=True)
    document_id = Column(Integer, ForeignKey("documents.id"))

    nomenclature_id = Column(Integer, ForeignKey("nomenclature.id"))

    serial_number = Column(String, nullable=True)  # Серийный номер (для оружия)
    quantity = Column(Integer, default=1)  # Количество (для патронов > 1, для оружия = 1)

    year_of_manufacture = Column(Integer, nullable=True)  # Год выпуска
    category = Column(Integer, default=1)  # Категория качества (1, 2, 3...)

    document = relationship("Document", back_populates="items")
    nomenclature = relationship("Nomenclature")