# app/arsenal/reports.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.database import get_arsenal_db
from app.core.auth import get_current_user
from app.modules.arsenal.models import Document, DocumentItem, Nomenclature, WeaponRegistry

router = APIRouter(prefix="/api/arsenal/reports", tags=["Arsenal Reports"])


# 1. Поиск оружия для выбора (чтобы пользователь мог выбрать, чью историю смотреть)
@router.get("/search-weapon")
async def search_weapon(
        q: str = Query(..., min_length=2, description="Поиск по серийному номеру или названию"),
        db: AsyncSession = Depends(get_arsenal_db),
        user=Depends(get_current_user)
):
    """Ищет уникальные пары (Номенклатура + Серия) во всей истории документов"""
    stmt = (
        select(DocumentItem.serial_number, Nomenclature.id, Nomenclature.name, Nomenclature.code)
        .join(Nomenclature, DocumentItem.nomenclature_id == Nomenclature.id)
        .where(
            (DocumentItem.serial_number.ilike(f"%{q}%")) |
            (Nomenclature.name.ilike(f"%{q}%"))
        )
        .distinct()
        .limit(20)
    )
    result = await db.execute(stmt)
    # Преобразуем в список словарей
    items = []
    for row in result.all():
        items.append({
            "serial": row.serial_number,
            "nom_id": row.id,
            "name": row.name,
            "code": row.code
        })
    return items


# 2. Получение полной истории (Таймлайн)
@router.get("/timeline")
async def get_weapon_timeline(
        serial: str,
        nom_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        user=Depends(get_current_user)
):
    """Собирает все движения конкретного ствола/партии"""

    # Запрос всех строк документов, где фигурировал этот предмет
    stmt = (
        select(DocumentItem)
        .join(Document, DocumentItem.document_id == Document.id)
        .options(
            selectinload(DocumentItem.document).selectinload(Document.source),
            selectinload(DocumentItem.document).selectinload(Document.target)
        )
        .where(
            DocumentItem.serial_number == serial,
            DocumentItem.nomenclature_id == nom_id
        )
        .order_by(Document.operation_date.desc(), Document.created_at.desc())
    )

    result = await db.execute(stmt)
    history = result.scalars().all()

    timeline = []
    for item in history:
        doc = item.document
        timeline.append({
            "date": doc.operation_date.strftime("%d.%m.%Y"),
            "doc_number": doc.doc_number,
            "op_type": doc.operation_type,
            "source": doc.source.name if doc.source else "Внешний источник",
            "target": doc.target.name if doc.target else "Списание / Вне учета",
            "quantity": item.quantity
        })

    # Получаем текущий статус (где лежит сейчас)
    reg_stmt = select(WeaponRegistry).options(selectinload(WeaponRegistry.current_object)).where(
        WeaponRegistry.serial_number == serial,
        WeaponRegistry.nomenclature_id == nom_id
    )
    reg_res = await db.execute(reg_stmt)
    current_state = reg_res.scalars().first()

    status_text = "Списано / Не на учете"
    if current_state and current_state.status == 1:
        loc = current_state.current_object.name if current_state.current_object else "?"
        status_text = f"В наличии: {loc}"

    return {
        "status": status_text,
        "history": timeline
    }