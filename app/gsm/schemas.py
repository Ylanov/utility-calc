from pydantic import BaseModel, condecimal, Field, validator
from typing import Optional, List
from datetime import datetime
from decimal import Decimal

# ======================================================
# КАСТОМНЫЕ ТИПЫ ДЛЯ ГСМ
# ======================================================

# Для объемов и массы топлива используем 3 знака после запятой (тысячные доли, напр. 15.235 т)
GsmVolume = condecimal(max_digits=15, decimal_places=3)


# ======================================================
# СХЕМЫ ДЛЯ ОБЪЕКТОВ (РЕЗЕРВУАРЫ, СКЛАДЫ, АТЗ)
# ======================================================

class ObjCreate(BaseModel):
    name: str
    obj_type: str
    parent_id: Optional[int] = None


class ObjResponse(BaseModel):
    id: int
    name: str
    obj_type: str
    parent_id: Optional[int]

    class Config:
        from_attributes = True


# ======================================================
# СХЕМЫ ДЛЯ НОМЕНКЛАТУРЫ (МАРКИ ТОПЛИВА И МАСЕЛ)
# ======================================================

class NomenclatureCreate(BaseModel):
    code: Optional[str] = None
    name: str
    category: Optional[str] = None

    # В JS мы оставили ключ is_numbered для совместимости интерфейса,
    # в БД ГСМ он будет интерпретироваться как is_packaged (фасованная продукция / бочки)
    is_numbered: bool = False


class NomenclatureResponse(BaseModel):
    id: int
    code: Optional[str]
    name: str
    category: Optional[str]
    is_packaged: bool

    class Config:
        from_attributes = True


# ======================================================
# СХЕМЫ ДЛЯ ДОКУМЕНТОВ (НАКЛАДНЫЕ, АКТЫ ПРИЕМА-ПЕРЕДАЧИ)
# ======================================================

class DocItemCreate(BaseModel):
    nomenclature_id: int

    # Фронтенд (JS) отправляет ключ serial_number (для ГСМ это номер Паспорта/Партии)
    serial_number: Optional[str] = None

    # Количество топлива (объем/масса). По умолчанию 0.000
    quantity: GsmVolume = Decimal("0.000")


class DocCreate(BaseModel):
    doc_number: Optional[str] = None
    operation_type: str
    source_id: Optional[int] = None
    target_id: Optional[int] = None
    operation_date: Optional[datetime] = None
    items: List[DocItemCreate]

    @validator("operation_date", pre=True, always=True)
    def normalize_date(cls, value):
        """
        Преобразует строку даты с фронтенда (YYYY-MM-DD) в объект datetime.
        Если дата не пришла — ставит текущее время.
        """
        if not value:
            return datetime.utcnow()

        if isinstance(value, str):
            # Если приходит только дата без времени (из input type="date")
            if len(value) == 10:
                return datetime.strptime(value, "%Y-%m-%d")

            # Если приходит полный ISO формат
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass

        return value


# ======================================================
# СХЕМЫ ДЛЯ ОТВЕТОВ ПО ДОКУМЕНТАМ (ДЛЯ ТАБЛИЦ И ПРОСМОТРА)
# ======================================================

class DocItemResponse(BaseModel):
    id: int
    nomenclature_id: int
    batch_number: Optional[str] = None
    quantity: Decimal

    class Config:
        from_attributes = True


class DocResponse(BaseModel):
    id: int
    doc_number: str
    operation_type: str
    operation_date: datetime
    source_name: Optional[str] = None
    target_name: Optional[str] = None
    items: List[DocItemResponse] = []

    class Config:
        from_attributes = True