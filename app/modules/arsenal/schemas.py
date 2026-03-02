from typing import List, Optional
from pydantic import BaseModel, validator
from datetime import datetime

class ObjCreate(BaseModel):
    name: str
    obj_type: str
    parent_id: Optional[int] = None
    mol_name: Optional[str] = None

class NomenclatureCreate(BaseModel):
    code: Optional[str] = None
    name: str
    category: Optional[str] = None
    is_numbered: bool = True
    default_account: Optional[str] = None

class DocItemCreate(BaseModel):
    nomenclature_id: int
    serial_number: Optional[str] = None
    quantity: int = 1
    inventory_number: Optional[str] = None
    price: Optional[float] = None

class DocCreate(BaseModel):
    doc_number: Optional[str] = None
    operation_type: str
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    operation_date: Optional[datetime] = None
    items: List[DocItemCreate]

    @validator("operation_date", pre=True, always=True)
    def normalize_date(cls, value):
        if not value:
            return datetime.utcnow()
        if isinstance(value, str):
            if len(value) == 10:
                return datetime.strptime(value, "%Y-%m-%d")
            return datetime.fromisoformat(value)
        return value