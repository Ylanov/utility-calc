# app/arsenal/routes.py
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from app.database import get_arsenal_db
from app.arsenal.models import AccountingObject, Nomenclature, Document, DocumentItem, OperationType


# --- Pydantic схемы (валидация данных) ---

class ObjCreate(BaseModel):
    name: str
    obj_type: str
    parent_id: Optional[int] = None


class DocItemCreate(BaseModel):
    nomenclature_id: int
    serial_number: Optional[str] = None
    quantity: int = 1


class DocCreate(BaseModel):
    doc_number: str
    operation_type: str  # Принимаем строку, pydantic/sqlalchemy сами разберутся с Enum если совпадает
    source_id: Optional[int]
    target_id: Optional[int]
    operation_date: Optional[datetime]
    items: List[DocItemCreate]


# --- Роутер ---

router = APIRouter(prefix="/api/arsenal", tags=["STROB Arsenal"])


# --- 1. Объекты учета (Склады, Подразделения) ---

@router.get("/objects")
async def get_objects(db: AsyncSession = Depends(get_arsenal_db)):
    """Получить список всех объектов учета"""
    result = await db.execute(select(AccountingObject))
    return result.scalars().all()


@router.post("/objects")
async def create_object(data: ObjCreate, db: AsyncSession = Depends(get_arsenal_db)):
    """Создать новый объект учета"""
    obj = AccountingObject(**data.dict())
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.delete("/objects/{obj_id}")
async def delete_object(obj_id: int, db: AsyncSession = Depends(get_arsenal_db)):
    """Удалить объект учета"""
    obj = await db.get(AccountingObject, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Объект не найден")

    # Проверка: нельзя удалять, если есть связанные документы
    # (упрощенно, в реальной системе нужна проверка связей)

    await db.delete(obj)
    await db.commit()
    return {"status": "deleted"}


# --- 2. Номенклатура (Справочник изделий) ---

@router.get("/nomenclature")
async def get_nomenclature(db: AsyncSession = Depends(get_arsenal_db)):
    """Получить список номенклатуры"""
    result = await db.execute(select(Nomenclature))
    return result.scalars().all()


# --- 3. Документы (Приход, Расход и т.д.) ---

@router.get("/documents")
async def get_documents(db: AsyncSession = Depends(get_arsenal_db)):
    """Получить журнал документов"""
    stmt = (
        select(Document)
        .options(selectinload(Document.source), selectinload(Document.target))
        .order_by(Document.created_at.desc())
    )
    result = await db.execute(stmt)
    docs = result.scalars().all()

    # Преобразуем в удобный формат для JSON ответа
    response_data = []
    for d in docs:
        response_data.append({
            "id": d.id,
            "doc_number": d.doc_number,
            "date": d.operation_date.strftime("%d.%m.%Y") if d.operation_date else "-",
            "type": d.operation_type.value if hasattr(d.operation_type, 'value') else str(d.operation_type),
            "source": d.source.name if d.source else "-",
            "target": d.target.name if d.target else "-",
            "comment": d.comment
        })
    return response_data


@router.post("/documents")
async def create_document(data: DocCreate, db: AsyncSession = Depends(get_arsenal_db)):
    """Создать новый документ"""
    try:
        # Создаем шапку документа
        new_doc = Document(
            doc_number=data.doc_number,
            operation_type=data.operation_type,
            source_id=data.source_id,
            target_id=data.target_id,
            operation_date=data.operation_date or datetime.utcnow()
        )
        db.add(new_doc)
        await db.flush()  # Получаем ID нового документа

        # Создаем строки (изделия в документе)
        for item in data.items:
            doc_item = DocumentItem(
                document_id=new_doc.id,
                nomenclature_id=item.nomenclature_id,
                serial_number=item.serial_number,
                quantity=item.quantity
            )
            db.add(doc_item)

        await db.commit()
        return {"status": "created", "id": new_doc.id}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: int, db: AsyncSession = Depends(get_arsenal_db)):
    """Удалить документ"""
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    await db.delete(doc)  # Строки удалятся каскадно (если настроено в моделях)
    await db.commit()
    return {"status": "deleted"}