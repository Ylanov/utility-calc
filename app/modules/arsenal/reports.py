from typing import Annotated
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import or_

from app.core.database import get_arsenal_db
from app.modules.arsenal.models import Document, DocumentItem, Nomenclature, WeaponRegistry, ArsenalUser
from app.modules.arsenal.deps import get_current_arsenal_user

router = APIRouter(tags=["Arsenal Reports"])


@router.get("/reports/search-weapon")
async def search_weapon(
        q: Annotated[str, Query(min_length=2, description="Поиск по серийнику, инв. номеру или названию")],
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)]
):
    """
    Поиск истории конкретного изделия/партии.
    Ищет по всем документам (сохраненная история).
    """
    # Ищем в истории документов (DocumentItem)
    stmt = (
        select(
            DocumentItem.serial_number,
            DocumentItem.inventory_number,
            Nomenclature.id.label("nom_id"),
            Nomenclature.name,
            Nomenclature.code
        )
        .join(Nomenclature, DocumentItem.nomenclature_id == Nomenclature.id)
        .where(
            or_(
                DocumentItem.serial_number.ilike(f"%{q}%"),
                DocumentItem.inventory_number.ilike(f"%{q}%"),
                Nomenclature.name.ilike(f"%{q}%")
            )
        )
        .distinct()
        .limit(20)
    )

    result = await db.execute(stmt)

    items = []
    for row in result.all():
        items.append({
            "serial": row.serial_number or "Б/Н",
            "inventory": row.inventory_number or "Б/Н",
            "nom_id": row.nom_id,
            "name": row.name,
            "code": row.code
        })
    return items


@router.get("/reports/timeline")
async def get_weapon_timeline(
        serial: str,
        nom_id: int,
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)],
        skip: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 50
):
    """
    Таймлайн (История движения) конкретного ствола/партии.
    """
    # 1. Запрашиваем историю из документов
    # Сортировка по дате операции (от новых к старым)
    stmt = (
        select(DocumentItem)
        .join(Document, DocumentItem.document_id == Document.id)
        .options(
            selectinload(DocumentItem.document).selectinload(Document.source),
            selectinload(DocumentItem.document).selectinload(Document.target)
            # УДАЛЕНО: selectinload(DocumentItem.document).selectinload(Document.author)
            # Причина: в модели Document пока нет relationship("author")
        )
        .where(
            # Обработка Б/Н (поиск может передавать строку "Б/Н")
            DocumentItem.serial_number == (None if serial == "Б/Н" else serial),
            DocumentItem.nomenclature_id == nom_id
        )
        .order_by(Document.operation_date.desc(), Document.created_at.desc())
        .offset(skip)
        .limit(limit)
    )

    result = await db.execute(stmt)
    history = result.scalars().all()

    timeline = []
    for item in history:
        doc = item.document
        timeline.append({
            "doc_id": doc.id,
            "date": doc.operation_date.strftime("%d.%m.%Y"),
            "doc_number": doc.doc_number,
            "op_type": doc.operation_type,
            "source": doc.source.name if doc.source else "Внешний источник / Списание",
            "target": doc.target.name if doc.target else "Списание / Вне учета",
            "quantity": item.quantity,
            "price": float(item.price) if item.price else 0.0,
            "inventory_number": item.inventory_number or "Б/Н"
        })

    # 2. Получаем текущий статус (Где это изделие лежит сейчас?)
    reg_stmt = select(WeaponRegistry).options(selectinload(WeaponRegistry.current_object)).where(
        WeaponRegistry.serial_number == (None if serial == "Б/Н" else serial),
        WeaponRegistry.nomenclature_id == nom_id
    )
    reg_res = await db.execute(reg_stmt)
    current_state = reg_res.scalars().first()

    status_text = "Списано / Вне баланса"
    if current_state:
        if current_state.status == 1:
            loc = current_state.current_object.name if current_state.current_object else "Неизвестно"
            status_text = f"В наличии: {loc}"
        elif current_state.status == 2:
            status_text = "В ремонте / В пути"

    return {
        "status": status_text,
        "history": timeline
    }