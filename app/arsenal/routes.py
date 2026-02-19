from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from typing import List, Optional
from pydantic import BaseModel, validator
from datetime import datetime

# ======================================================
# ИМПОРТЫ ДЛЯ АУТЕНТИФИКАЦИИ
# ======================================================
from app.database import get_arsenal_db
from app.auth import get_current_user  # <-- 1. ДОБАВЛЕН ИМПОРТ
# ======================================================

from app.arsenal.models import (
    AccountingObject,
    Nomenclature,
    Document,
    DocumentItem,
    WeaponRegistry
)
from app.arsenal.services import WeaponService


# ======================================================
# PYDANTIC СХЕМЫ (Валидация входящих данных)
# ======================================================

class ObjCreate(BaseModel):
    name: str
    obj_type: str
    parent_id: Optional[int] = None


class NomenclatureCreate(BaseModel):
    code: Optional[str] = None
    name: str
    category: Optional[str] = None
    # Флаг: True - номерной учет (автоматы), False - партионный (патроны)
    is_numbered: bool = True


class DocItemCreate(BaseModel):
    nomenclature_id: int
    serial_number: Optional[str] = None
    quantity: int = 1


class DocCreate(BaseModel):
    doc_number: str
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
            # Если приходит только дата без времени
            if len(value) == 10:
                return datetime.strptime(value, "%Y-%m-%d")

            # Если ISO формат
            return datetime.fromisoformat(value)

        return value


# ======================================================
# РОУТЕР
# ======================================================

router = APIRouter(prefix="/api/arsenal", tags=["STROB Arsenal"])


# ======================================================
# 1. ОБЪЕКТЫ УЧЕТА (Склады, Подразделения)
# ======================================================

@router.get("/objects")
async def get_objects(
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """Получить список всех объектов учета"""
    result = await db.execute(
        select(AccountingObject).order_by(AccountingObject.name)
    )
    return result.scalars().all()


@router.post("/objects")
async def create_object(
    data: ObjCreate,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """Создать новый объект учета"""
    existing = await db.execute(
        select(AccountingObject).where(AccountingObject.name == data.name)
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Объект с таким именем уже существует"
        )

    obj = AccountingObject(**data.dict())
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


@router.delete("/objects/{obj_id}")
async def delete_object(
    obj_id: int,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """Удалить объект учета"""
    obj = await db.get(AccountingObject, obj_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Объект не найден")

    await db.delete(obj)
    await db.commit()
    return {"status": "deleted"}


# ======================================================
# 2. НОМЕНКЛАТУРА (Справочник изделий)
# ======================================================

@router.get("/nomenclature")
async def get_nomenclature(
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """Получить список номенклатуры"""
    result = await db.execute(
        select(Nomenclature).order_by(Nomenclature.name)
    )
    return result.scalars().all()


@router.post("/nomenclature")
async def create_nomenclature(
    data: NomenclatureCreate,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """Добавить новый тип вооружения или боеприпасов"""
    existing = await db.execute(
        select(Nomenclature).where(Nomenclature.name == data.name)
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=400,
            detail="Изделие с таким наименованием уже существует"
        )

    new_item = Nomenclature(**data.dict())
    db.add(new_item)
    await db.commit()
    await db.refresh(new_item)
    return new_item


# ======================================================
# 3. ДОКУМЕНТЫ (Приход, Перемещение, Списание)
# ======================================================

@router.get("/documents")
async def get_documents(
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """Получить журнал документов"""
    stmt = (
        select(Document)
        .options(
            selectinload(Document.source),
            selectinload(Document.target)
        )
        .order_by(
            Document.operation_date.desc(),
            Document.created_at.desc()
        )
    )

    result = await db.execute(stmt)
    docs = result.scalars().all()

    response_data = []
    for d in docs:
        response_data.append({
            "id": d.id,
            "doc_number": d.doc_number,
            "date": d.operation_date.strftime("%d.%m.%Y")
            if d.operation_date else "-",
            "type": d.operation_type,
            "source": d.source.name if d.source else "-",
            "target": d.target.name if d.target else "-"
        })

    return response_data


@router.get("/documents/{doc_id}")
async def get_document_details(
    doc_id: int,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """Получить подробную информацию о документе"""
    stmt = (
        select(Document)
        .where(Document.id == doc_id)
        .options(
            selectinload(Document.source),
            selectinload(Document.target),
            selectinload(Document.items)
            .selectinload(DocumentItem.nomenclature),
            selectinload(Document.items)
            .selectinload(DocumentItem.weapon)
        )
    )
    doc = (await db.execute(stmt)).scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    return doc


@router.post("/documents")
async def create_document(
    data: DocCreate,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """
    Создать документ с автоматической проводкой по реестру оружия.
    Операция выполняется атомарно через WeaponService.
    """
    try:
        # Вся бизнес-логика (включая партионный учет) инкапсулирована в сервисе
        new_doc = await WeaponService.process_document(
            db,
            data,
            data.items
        )

        return {
            "status": "created",
            "id": new_doc.id
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        await db.rollback()
        # В реальном продакшене здесь нужно логирование (logger.error)
        raise HTTPException(
            status_code=400,
            detail=f"Ошибка проведения документа: {str(e)}"
        )


@router.delete("/documents/{doc_id}")
async def delete_document(
    doc_id: int,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """
    Удалить документ.
    Внимание: это не выполняет сторнирование движений (возврат остатков).
    Для полноценного учета нужно делать сторнирующие документы, но для MVP удаляем.
    """
    doc = await db.get(Document, doc_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    # В будущем здесь можно добавить проверку прав:
    # if current_user.role != "admin":
    #     raise HTTPException(status_code=403, detail="Недостаточно прав для удаления")

    await db.delete(doc)
    await db.commit()
    return {"status": "deleted"}


# ======================================================
# 4. ОСТАТКИ (РЕЕСТР)
# ======================================================

@router.get("/balance/{obj_id}")
async def get_object_balance(
    obj_id: int,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user=Depends(get_current_user)  # <-- 2. ДОБАВЛЕНА ЗАЩИТА
):
    """
    Получить текущие остатки по объекту.
    Берем данные напрямую из WeaponRegistry.
    Учитывает и поштучный, и партионный учет.
    """
    stmt = (
        select(WeaponRegistry)
        .join(Nomenclature)  # Джойн для сортировки по имени
        .options(selectinload(WeaponRegistry.nomenclature))
        .where(
            WeaponRegistry.current_object_id == obj_id,
            WeaponRegistry.status == 1
        )
        .order_by(Nomenclature.name, WeaponRegistry.serial_number)
    )

    weapons = (await db.execute(stmt)).scalars().all()

    balance = []
    for weapon in weapons:
        # Определяем, как отображать серийник
        is_numbered = weapon.nomenclature.is_numbered
        display_serial = weapon.serial_number

        # Если учет партионный, серийник - это номер партии
        if not is_numbered:
            display_serial = f"Партия {weapon.serial_number}"

        balance.append({
            "nomenclature": weapon.nomenclature.name,
            "code": weapon.nomenclature.code,
            "serial_number": display_serial,
            "quantity": weapon.quantity,  # Теперь здесь реальное количество
            "is_numbered": is_numbered
        })

    return balance