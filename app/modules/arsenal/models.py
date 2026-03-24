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

    source = relationship("AccountingObject", foreign_keys=[source_id])
    target = relationship("AccountingObject", foreign_keys=[target_id])

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
