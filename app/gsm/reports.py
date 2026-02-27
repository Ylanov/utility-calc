# app/gsm/reports.py
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import desc

from app.database import get_gsm_db
from app.gsm.routes import get_current_gsm_user
from app.gsm.models import GsmDocument, GsmDocumentItem, GsmNomenclature, FuelRegistry

router = APIRouter(prefix="/api/gsm/reports", tags=["GSM Reports"])


# 1. Поиск партии / паспорта качества (для автокомплита в поиске)
@router.get("/search")
async def search_batch(
        q: str = Query(..., min_length=2, description="Поиск по паспорту качества или марке ГСМ"),
        db: AsyncSession = Depends(get_gsm_db),
        user=Depends(get_current_gsm_user)
):
    """Ищет уникальные пары (Марка + Паспорт) во всей истории документов ГСМ"""
    stmt = (
        select(GsmDocumentItem.batch_number, GsmNomenclature.id, GsmNomenclature.name, GsmNomenclature.code)
        .join(GsmNomenclature, GsmDocumentItem.nomenclature_id == GsmNomenclature.id)
        .where(
            (GsmDocumentItem.batch_number.ilike(f"%{q}%")) |
            (GsmNomenclature.name.ilike(f"%{q}%"))
        )
        .distinct()
        .limit(20)
    )
    result = await db.execute(stmt)

    # Преобразуем в список словарей для фронтенда
    items = []
    for row in result.all():
        items.append({
            "serial": row.batch_number,  # JS ждет ключ "serial", хотя это "batch"
            "nom_id": row.id,
            "name": row.name,
            "code": row.code
        })
    return items


# 2. Получение полной истории (Таймлайн движения партии)
@router.get("/timeline")
async def get_fuel_timeline(
        serial: str,  # Это номер паспорта/партии (приходит как serial из JS)
        nom_id: int,
        db: AsyncSession = Depends(get_gsm_db),
        user=Depends(get_current_gsm_user)
):
    """Собирает все движения конкретной партии топлива или бочки"""

    # Запрос всех строк документов, где фигурировала эта партия
    stmt = (
        select(GsmDocumentItem)
        .join(GsmDocument, GsmDocumentItem.document_id == GsmDocument.id)
        .options(
            selectinload(GsmDocumentItem.document).selectinload(GsmDocument.source),
            selectinload(GsmDocumentItem.document).selectinload(GsmDocument.target)
        )
        .where(
            GsmDocumentItem.batch_number == serial,
            GsmDocumentItem.nomenclature_id == nom_id
        )
        .order_by(GsmDocument.operation_date.desc(), GsmDocument.created_at.desc())
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
            "source": doc.source.name if doc.source else "Внешний поставщик",
            "target": doc.target.name if doc.target else "Списание / Расход",
            "quantity": item.quantity  # Объем операции
        })

    # Получаем текущий статус (где остатки этой партии сейчас)
    reg_stmt = select(FuelRegistry).options(selectinload(FuelRegistry.current_object)).where(
        FuelRegistry.batch_number == serial,
        FuelRegistry.nomenclature_id == nom_id,
        FuelRegistry.status == 1  # Только активные остатки
    )
    reg_res = await db.execute(reg_stmt)
    active_balances = reg_res.scalars().all()

    if not active_balances:
        status_text = "Партия полностью израсходована / Списана"
    else:
        # Партия может быть раскидана по нескольким резервуарам
        locations = []
        for b in active_balances:
            loc_name = b.current_object.name if b.current_object else "?"
            locations.append(f"{loc_name} ({b.quantity} л)")
        status_text = "В наличии: " + ", ".join(locations)

    return {
        "status": status_text,
        "history": timeline
    }