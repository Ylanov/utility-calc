from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    DateTime,
    Boolean,
    Text,
    Numeric,
    UniqueConstraint
)
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import GsmBase  # Убедитесь, что создали GsmBase в database.py


# --- Пользователи ГСМ ---
class GsmUser(GsmBase):
    __tablename__ = "gsm_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)

    # Роль: admin (центр), storage_head (начальник склада/резервуарного парка)
    role = Column(String, default="storage_head")

    # Привязка к конкретному складу/резервуару/АЗС
    object_id = Column(Integer, ForeignKey("gsm_accounting_objects.id"), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Связи
    accounting_object = relationship("GsmAccountingObject")


# --- Справочник: Организации / Объекты инфраструктуры ГСМ ---
class GsmAccountingObject(GsmBase):
    __tablename__ = "gsm_accounting_objects"

    id = Column(Integer, primary_key=True, index=True)
    parent_id = Column(Integer, ForeignKey("gsm_accounting_objects.id"), nullable=True)
    name = Column(String, nullable=False)

    # Тип: Склад, Резервуарный парк, АТЗ (топливозаправщик), Подразделение, Контрагент
    obj_type = Column(String, nullable=False)

    # Иерархия объектов (Self-referential relationship)
    children = relationship("GsmAccountingObject", backref="parent", remote_side=[id])


# --- Справочник: Номенклатура (Марки топлива и масел) ---
class GsmNomenclature(GsmBase):
    __tablename__ = "gsm_nomenclature"

    id = Column(Integer, primary_key=True, index=True)

    # ГОСТ или внутренний код (например: ГОСТ 32511-2013)
    code = Column(String, index=True)

    # Наименование (например: ДТ-Л-К5)
    name = Column(String, nullable=False)

    # Категория (Светлые нефтепродукты, Темные, Масла, Спецжидкости)
    category = Column(String, nullable=True)

    # 🔥 ФЛАГ ТИПА ПРОДУКЦИИ
    # False = Налив (Топливо в резервуарах). Учет ведется по объему/массе (дроби).
    # True  = Фасованная (Масла в бочках/канистрах). Учет ведется в штуках тары.
    is_packaged = Column(Boolean, default=False, nullable=False)


# --- ГЛАВНАЯ ТАБЛИЦА: РЕЕСТР ОСТАТКОВ ГСМ (РЕЗЕРВУАРЫ) ---
class FuelRegistry(GsmBase):
    __tablename__ = "gsm_fuel_registry"

    id = Column(Integer, primary_key=True, index=True)

    nomenclature_id = Column(Integer, ForeignKey("gsm_nomenclature.id"), nullable=False)

    # Номер паспорта качества или номер партии
    batch_number = Column(String, nullable=False, index=True)

    # Плотность ГСМ (кг/л). Важно для перевода объема в массу.
    density = Column(Numeric(10, 4), nullable=True)

    # Текущее местонахождение (В каком резервуаре/на складе находится)
    current_object_id = Column(Integer, ForeignKey("gsm_accounting_objects.id"), nullable=True)

    # Статус:
    # 1 - В наличии (Активный остаток)
    # 0 - Списано / Израсходовано (Историческая запись)
    status = Column(Integer, default=1)

    # 🔥 КОЛИЧЕСТВО (ОБЪЕМ / МАССА)
    # Используем Numeric(15, 3) для точного хранения дробей (до тысячных долей литра/кг)
    quantity = Column(Numeric(15, 3), default=0.000)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Связи
    nomenclature = relationship("GsmNomenclature")
    current_object = relationship("GsmAccountingObject")

    # Уникальность: (Марка топлива + Партия + Резервуар)
    # Нельзя иметь две одинаковые партии одного топлива в одном резервуаре разными строками.
    # Они должны суммироваться в поле quantity.
    __table_args__ = (
        UniqueConstraint(
            "nomenclature_id",
            "batch_number",
            "current_object_id",
            name="uix_gsm_nom_batch_obj"
        ),
    )


# --- Документы (Накладные, Акты приема-передачи, Раздаточные ведомости) ---
class GsmDocument(GsmBase):
    __tablename__ = "gsm_documents"

    id = Column(Integer, primary_key=True, index=True)

    # Номер документа (Акта/Накладной)
    doc_number = Column(String, index=True)

    doc_date = Column(DateTime, default=datetime.utcnow)
    operation_date = Column(DateTime, default=datetime.utcnow)

    # Тип операции: 'Первичный ввод', 'Перемещение', 'Прием', 'Отправка', 'Списание'
    operation_type = Column(String, nullable=False)

    # Отправитель (Поставщик / Резервуар выдачи)
    source_id = Column(Integer, ForeignKey("gsm_accounting_objects.id"), nullable=True)

    # Получатель (Резервуар приема / Техника)
    target_id = Column(Integer, ForeignKey("gsm_accounting_objects.id"), nullable=True)

    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    author_id = Column(Integer, ForeignKey("gsm_users.id"), nullable=True)

    # Связи
    source = relationship("GsmAccountingObject", foreign_keys=[source_id])
    target = relationship("GsmAccountingObject", foreign_keys=[target_id])
    items = relationship(
        "GsmDocumentItem",
        back_populates="document",
        cascade="all, delete"
    )


# --- Состав документа (Строки накладной на ГСМ) ---
class GsmDocumentItem(GsmBase):
    __tablename__ = "gsm_document_items"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(Integer, ForeignKey("gsm_documents.id"))

    # Ссылка на конкретную партию в реестре (может быть NULL при полном списании)
    fuel_id = Column(Integer, ForeignKey("gsm_fuel_registry.id"), nullable=True)

    # Дублирование данных для сохранения исторического следа
    nomenclature_id = Column(Integer, ForeignKey("gsm_nomenclature.id"))

    # Номер паспорта качества / Партии
    batch_number = Column(String, nullable=True)

    # Объем или масса переданного ГСМ (Дробное число)
    quantity = Column(Numeric(15, 3), default=0.000)

    # Связи
    document = relationship("GsmDocument", back_populates="items")
    nomenclature = relationship("GsmNomenclature")
    fuel = relationship("FuelRegistry")
