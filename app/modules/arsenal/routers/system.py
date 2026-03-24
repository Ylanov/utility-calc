from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func

from app.core.database import get_arsenal_db
from app.modules.arsenal.models import ArsenalUser, WeaponRegistry, Document
from app.modules.arsenal.deps import get_current_arsenal_user
from app.modules.arsenal.services.excel_import import import_arsenal_from_excel

router = APIRouter(tags=["Arsenal System"])


@router.post(
    "/import",
    responses={
        400: {"description": "Ошибка формата или данных файла"},
        403: {"description": "Доступ запрещен"}
    }
)
async def import_excel_data(
        # ИСПОЛЬЗУЕМ ANNOTATED: это уберет красные подчеркивания в PyCharm
        file: Annotated[UploadFile, File(...)],
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)]
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может выполнять массовый импорт данных")

    if not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Неверный формат. Пожалуйста, загрузите файл формата Excel")

    # Читаем файл в память
    file_bytes = await file.read()

    # Делегируем сложную логику парсинга сервисной функции
    result = await import_arsenal_from_excel(file_bytes, db)

    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["message"])

    return result


@router.get("/kpi")
async def get_dashboard_kpi(
        # ИСПОЛЬЗУЕМ ANNOTATED ЗДЕСЬ ТОЖЕ
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)]
):
    """
    Возвращает ключевые показатели (KPI) для дашборда.
    Кэширование отключено для отображения данных в реальном времени.
    """

    # 1. Запрос для активного оружия (В наличии)
    active_stmt = select(
        func.coalesce(func.sum(WeaponRegistry.quantity), 0).label("qty"),
        func.coalesce(
            func.sum(func.coalesce(WeaponRegistry.price, 0) * WeaponRegistry.quantity), 0
        ).label("total_price")
    ).where(WeaponRegistry.status == 1)

    # 2. Запрос для ремонта/в пути
    repair_stmt = select(
        func.coalesce(func.sum(WeaponRegistry.quantity), 0)
    ).where(WeaponRegistry.status == 2)

    # 3. Запрос количества документов
    doc_stmt = select(func.count(Document.id))

    # Если это начальник склада (unit_head) — показываем цифры только ЕГО склада.
    if current_user.role == "unit_head":
        active_stmt = active_stmt.where(WeaponRegistry.current_object_id == current_user.object_id)
        repair_stmt = repair_stmt.where(WeaponRegistry.current_object_id == current_user.object_id)
        doc_stmt = doc_stmt.where(
            (Document.source_id == current_user.object_id) |
            (Document.target_id == current_user.object_id)
        )

    # Выполняем запросы
    active_res = (await db.execute(active_stmt)).first()
    repair_qty = (await db.execute(repair_stmt)).scalar()
    docs_count = (await db.execute(doc_stmt)).scalar()

    return {
        "total_qty": active_res.qty,
        "total_sum": float(active_res.total_price),
        "transit_qty": repair_qty,
        "docs_count": docs_count
    }
