from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    DateTime,
    Boolean,
    Text,
    UniqueConstraint,
    Numeric,
    Index
)
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import ArsenalBase


# =====================================================================
# DB CONSTANTS (ENTERPRISE)
# =====================================================================
ACCOUNTING_OBJECT_FK = "accounting_objects.id"
ARSENAL_USER_FK = "arsenal_users.id"
NOMENCLATURE_FK = "nomenclature.id"
DOCUMENT_FK = "documents.id"
WEAPON_REGISTRY_FK = "weapon_registry.id"


# =====================================================================
# USERS
# =====================================================================
class ArsenalUser(ArsenalBase):
    __tablename__ = "arsenal_users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)

    role = Column(String, default="unit_head")

    object_id = Column(Integer, ForeignKey(ACCOUNTING_OBJECT_FK), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    accounting_object = relationship("AccountingObject")


# =====================================================================
# ACCOUNTING OBJECTS
# =====================================================================
class AccountingObject(ArsenalBase):
    __tablename__ = "accounting_objects"

    id = Column(Integer, primary_key=True, index=True)

    parent_id = Column(Integer, ForeignKey(ACCOUNTING_OBJECT_FK), nullable=True)

    name = Column(String, nullable=False, index=True)
    obj_type = Column(String, nullable=False)

    mol_name = Column(String, nullable=True)

    children = relationship("AccountingObject", backref="parent", remote_side=[id])


# =====================================================================
# NOMENCLATURE
# =====================================================================
class Nomenclature(ArsenalBase):
    __tablename__ = "nomenclature"

    id = Column(Integer, primary_key=True, index=True)

    code = Column(String, index=True)
    name = Column(String, nullable=False, index=True)
    category = Column(String, nullable=True)

    default_account = Column(String, nullable=True)

    is_numbered = Column(Boolean, default=True, nullable=False)

    # Минимальный остаток — для алертов «заканчивается» (только для is_numbered=False).
    # 0 = без порога (по умолчанию). Если текущее qty < min_quantity → появляется
    # запись в /arsenal/alerts/low-stock.
    min_quantity = Column(Integer, nullable=False, default=0)


# =====================================================================
# WEAPON REGISTRY
# =====================================================================
class WeaponRegistry(ArsenalBase):
    __tablename__ = "weapon_registry"

    id = Column(Integer, primary_key=True, index=True)

    nomenclature_id = Column(Integer, ForeignKey(NOMENCLATURE_FK), nullable=False)

    serial_number = Column(String, nullable=False, index=True)
    year_of_manufacture = Column(Integer, nullable=True)

    current_object_id = Column(Integer, ForeignKey(ACCOUNTING_OBJECT_FK), nullable=True)

    status = Column(Integer, default=1)
    quantity = Column(Integer, default=1)

    inventory_number = Column(String, index=True, nullable=True)

    price = Column(Numeric(15, 2), nullable=True)
    account_code = Column(String, nullable=True)
    kbk = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    nomenclature = relationship("Nomenclature")
    current_object = relationship("AccountingObject")

    __table_args__ = (
        UniqueConstraint(
            "nomenclature_id",
            "serial_number",
            "current_object_id",
            name="uix_nom_serial_obj"
        ),
        Index("ix_weapon_object_status", "current_object_id", "status"),
        Index("ix_weapon_status_qty_price", "status", "quantity", "price"),
    )


# =====================================================================
# DOCUMENTS
# =====================================================================
class Document(ArsenalBase):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    doc_number = Column(String, index=True)

    doc_date = Column(DateTime, default=datetime.utcnow)
    operation_date = Column(DateTime, default=datetime.utcnow)

    operation_type = Column(String, nullable=False)

    source_id = Column(Integer, ForeignKey(ACCOUNTING_OBJECT_FK), nullable=True)
    target_id = Column(Integer, ForeignKey(ACCOUNTING_OBJECT_FK), nullable=True)

    comment = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    author_id = Column(Integer, ForeignKey(ARSENAL_USER_FK), nullable=True)

    attached_file_path = Column(String, nullable=True)

    # Rollback-трекинг: если документ отменён обратным документом (reversal),
    # is_reversed=True и reversed_by_document_id указывает на reversal.
    # Удалять исходный документ нельзя — у военного учёта должна быть аудируемая
    # история «проведено → отменено», не физическое удаление.
    is_reversed = Column(Boolean, default=False, nullable=False, index=True)
    reversed_by_document_id = Column(Integer, ForeignKey(DOCUMENT_FK), nullable=True)
    # Обратная ссылка: если это reversal, указывает на оригинал.
    reverses_document_id = Column(Integer, ForeignKey(DOCUMENT_FK), nullable=True)

    # Для операции «Списание» — причина (FK на справочник DisposalReason).
    # Опционально, но новые документы-списания должны его иметь (валидация в service).
    disposal_reason_id = Column(Integer, ForeignKey("disposal_reasons.id"), nullable=True)

    source = relationship("AccountingObject", foreign_keys=[source_id])
    target = relationship("AccountingObject", foreign_keys=[target_id])
    author = relationship("ArsenalUser", foreign_keys=[author_id])
    disposal_reason = relationship("DisposalReason", foreign_keys=[disposal_reason_id])

    items = relationship(
        "DocumentItem",
        back_populates="document",
        cascade="all, delete"
    )

    __table_args__ = (
        Index("ix_doc_source_date", "source_id", "operation_date"),
        Index("ix_doc_target_date", "target_id", "operation_date"),
        Index("ix_doc_dates", "operation_date", "created_at"),
    )


# =====================================================================
# DOCUMENT ITEMS
# =====================================================================
class DocumentItem(ArsenalBase):
    __tablename__ = "document_items"

    id = Column(Integer, primary_key=True, index=True)

    document_id = Column(Integer, ForeignKey(DOCUMENT_FK))
    weapon_id = Column(Integer, ForeignKey(WEAPON_REGISTRY_FK), nullable=True)

    nomenclature_id = Column(Integer, ForeignKey(NOMENCLATURE_FK))

    serial_number = Column(String, nullable=True)

    inventory_number = Column(String, nullable=True)
    price = Column(Numeric(15, 2), nullable=True)

    quantity = Column(Integer, default=1)

    document = relationship("Document", back_populates="items")
    nomenclature = relationship("Nomenclature")
    weapon = relationship("WeaponRegistry")

    __table_args__ = (
        Index("ix_doc_item_doc_id", "document_id"),
        Index("ix_doc_item_serial_nom", "serial_number", "nomenclature_id"),
    )


# =====================================================================
# DISPOSAL REASON — справочник причин списания / утилизации
# =====================================================================
# Раньше списание просто ставило status=0 у единицы имущества. Непонятно
# что именно произошло: утилизация из-за поломки, передача вышестоящей
# организации, утрата, конец срока эксплуатации, замена.
# Теперь при «Списание» / «Утилизация» документ ссылается на причину.
# Это даёт отчётность: «за год N списано по поломке, M — по сроку и т.д.»
class DisposalReason(ArsenalBase):
    __tablename__ = "disposal_reasons"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(32), unique=True, nullable=False)      # напр. "BREAKDOWN", "WEAR_OUT", "LOST"
    name = Column(String, nullable=False)                       # «Поломка», «Износ», «Утрата»
    # Куда делось имущество: 'disposal' (утилизировано) | 'external' (передано наружу)
    # | 'lost' (утрата) | 'other'. Для финансовой отчётности и ACL.
    kind = Column(String(16), nullable=False, default="disposal")
    is_active = Column(Boolean, default=True, nullable=False)


# =====================================================================
# INVENTORY — инвентаризационная проверка
# =====================================================================
# Раньше ничего не связывало физическое наличие на складе и запись в БД.
# Инвентаризация — это процесс: начали (открыли сессию) → ходим и сканируем
# серийники / вводим количества партий → система сравнивает с ожидаемым →
# формирует отчёт расхождений → админ утверждает закрытие, что автоматически
# создаёт корректирующие документы (списание недостачи / оприходование излишка).
class Inventory(ArsenalBase):
    __tablename__ = "inventories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    object_id = Column(Integer, ForeignKey(ACCOUNTING_OBJECT_FK), nullable=False, index=True)
    # 'open' — идёт сканирование, 'closed' — завершена (с документами расхождений),
    # 'cancelled' — админ отменил (расхождения игнорируются).
    status = Column(String(16), default="open", nullable=False, index=True)
    started_by_id = Column(Integer, ForeignKey(ARSENAL_USER_FK), nullable=True)
    closed_by_id = Column(Integer, ForeignKey(ARSENAL_USER_FK), nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)

    # Документы корректировок, созданных при закрытии (расхождения).
    correction_document_id = Column(Integer, ForeignKey(DOCUMENT_FK), nullable=True)

    object = relationship("AccountingObject", foreign_keys=[object_id])
    started_by = relationship("ArsenalUser", foreign_keys=[started_by_id])
    closed_by = relationship("ArsenalUser", foreign_keys=[closed_by_id])
    items = relationship(
        "InventoryItem", back_populates="inventory", cascade="all, delete"
    )


class InventoryItem(ArsenalBase):
    """Единица, зафиксированная в ходе инвентаризации.

    Для номерного учёта: одна строка = один серийник.
    Для партионного: одна строка на номенклатуру — поле found_quantity.

    Сравнение с БД:
       found_quantity vs expected_quantity (берётся из WeaponRegistry на момент
       закрытия). Разница > 0 → излишек, < 0 → недостача.
    """
    __tablename__ = "inventory_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    inventory_id = Column(Integer, ForeignKey("inventories.id"), nullable=False, index=True)
    nomenclature_id = Column(Integer, ForeignKey(NOMENCLATURE_FK), nullable=False)
    serial_number = Column(String, nullable=True)  # None для партионного
    found_quantity = Column(Integer, nullable=False, default=1)
    note = Column(Text, nullable=True)
    scanned_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    scanned_by_id = Column(Integer, ForeignKey(ARSENAL_USER_FK), nullable=True)

    inventory = relationship("Inventory", back_populates="items")
    nomenclature = relationship("Nomenclature")

    __table_args__ = (
        Index("ix_inventory_item_scan", "inventory_id", "nomenclature_id"),
    )


# =====================================================================
# PASSWORD RESET TOKEN — безопасный сброс пароля без JSON-plaintext
# =====================================================================
# Раньше POST /users/{id}/reset-password возвращал новый пароль в ответе —
# остаётся в логах proxy, истории браузера и т.д. Теперь генерируется
# одноразовый токен, ссылка с которым отдаётся админу. Пользователь сам
# устанавливает пароль по этой ссылке. Токен действует 24 часа.
class ArsenalPasswordResetToken(ArsenalBase):
    __tablename__ = "arsenal_password_reset_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey(ARSENAL_USER_FK), nullable=False, index=True)
    token_hash = Column(String(128), unique=True, nullable=False)  # sha256(token)
    expires_at = Column(DateTime, nullable=False, index=True)
    used_at = Column(DateTime, nullable=True)
    created_by_id = Column(Integer, ForeignKey(ARSENAL_USER_FK), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
