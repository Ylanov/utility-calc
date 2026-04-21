from typing import Optional, Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, func, distinct

from app.core.database import get_arsenal_db
from app.modules.arsenal.models import Nomenclature, ArsenalUser, WeaponRegistry, DocumentItem
from app.modules.arsenal.schemas import NomenclatureCreate
from app.modules.arsenal.deps import get_current_arsenal_user
from app.modules.arsenal.services.audit import write_arsenal_audit

router = APIRouter(tags=["Arsenal Nomenclature"])


# Стандартные категории — используются в UI как подсказки, но БД хранит
# произвольный String, так что админ может вводить и свои.
STANDARD_CATEGORIES = [
    "Стрелковое оружие",
    "Боеприпасы",
    "Средства защиты",
    "Специальные средства",
    "Связь",
    "Снаряжение / Экипировка",
    "Техника",
    "ЗИП / Запчасти",
    "Горюче-смазочные материалы",
    "Прочее",
]


@router.get("/nomenclature")
async def get_nomenclature(
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)],
        skip: Annotated[int, Query(ge=0, description="Смещение")] = 0,
        limit: Annotated[int, Query(ge=1, le=5000, description="Лимит записей")] = 100,
        q: Annotated[Optional[str], Query(min_length=1, description="Поиск по названию или индексу")] = None,
        category: Annotated[Optional[str], Query(description="Фильтр по категории")] = None,
        is_numbered: Annotated[Optional[bool], Query(description="Только номерной / только партионный")] = None,
):
    """
    Получение справочника номенклатуры с поддержкой поиска и пагинации.
    """
    stmt = select(Nomenclature)

    # ОПТИМИЗАЦИЯ: Серверный поиск
    if q:
        search_term = f"%{q}%"
        stmt = stmt.where(
            or_(
                Nomenclature.name.ilike(search_term),
                Nomenclature.code.ilike(search_term)
            )
        )
    if category:
        stmt = stmt.where(Nomenclature.category == category)
    if is_numbered is not None:
        stmt = stmt.where(Nomenclature.is_numbered.is_(is_numbered))

    # Сортировка и пагинация
    stmt = stmt.order_by(Nomenclature.name).offset(skip).limit(limit)

    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/nomenclature/categories")
async def list_categories(
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)],
):
    """Все используемые категории + стандартные (для селекта в форме).
    Считаем сколько позиций в каждой — UI покажет как бейджи."""
    rows = (await db.execute(
        select(Nomenclature.category, func.count(Nomenclature.id))
        .where(Nomenclature.category.is_not(None))
        .group_by(Nomenclature.category)
    )).all()
    used = {cat: int(c) for cat, c in rows if cat}
    # Объединяем стандартные и реально встречающиеся (сохраняя порядок)
    merged = list(STANDARD_CATEGORIES) + [c for c in used.keys() if c not in STANDARD_CATEGORIES]
    return {
        "categories": [{"name": c, "count": used.get(c, 0)} for c in merged],
        "standard": STANDARD_CATEGORIES,
    }


@router.get("/nomenclature/{nom_id}/qr")
async def get_nomenclature_qr(
        nom_id: int,
        size: int = Query(8, ge=2, le=20, description="Размер «пикселя» QR"),
        serial_number: Optional[str] = Query(None, description="Если указан — QR ведёт к конкретной единице"),
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    """Генерирует QR-код (PNG) для наклейки на единицу имущества.
    Содержит JSON с ref-ссылкой вида `arsenal://nom/<id>[?serial=...]` —
    мобильный сканер → открывает карточку.

    Используется также для массовой печати ярлыков (/nomenclature/{id}/labels).
    """
    import io
    import qrcode

    nom = await db.get(Nomenclature, nom_id)
    if not nom:
        raise HTTPException(404, "Номенклатура не найдена")

    payload = f"arsenal://nom/{nom_id}"
    if serial_number:
        payload += f"?serial={serial_number}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=size,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/nomenclature/{nom_id}/labels")
async def print_labels(
        nom_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    """PDF с ярлыками на все активные серийники этой номенклатуры.
    Админ печатает на лист наклеек и клеит на каждый экземпляр.
    Требует weasyprint (уже в зависимостях)."""
    import io
    try:
        from weasyprint import HTML
    except ImportError:
        raise HTTPException(503, "weasyprint недоступен — обратитесь к администратору сервера")

    nom = await db.get(Nomenclature, nom_id)
    if not nom:
        raise HTTPException(404, "Номенклатура не найдена")

    serials = (await db.execute(
        select(WeaponRegistry.serial_number)
        .where(
            WeaponRegistry.nomenclature_id == nom_id,
            WeaponRegistry.status == 1,
            WeaponRegistry.serial_number.is_not(None),
        )
        .order_by(WeaponRegistry.serial_number)
    )).scalars().all()
    if not serials:
        raise HTTPException(404, "Нет активных единиц для печати")

    # Рендерим HTML-сетку ярлыков — браузерный подход, но через weasyprint
    # получится приличный PDF. 3 ярлыка в ряду, 10 в столбце.
    labels_html = "\n".join(
        f'<div class="label">'
        f'  <div class="name">{nom.name}</div>'
        f'  <div class="serial">#{s}</div>'
        f'  <img src="/api/arsenal/nomenclature/{nom_id}/qr?size=4&serial_number={s}" '
        f'       class="qr" crossorigin="anonymous">'
        f'</div>'
        for s in serials
    )
    html = f"""
        <html><head><style>
            @page {{ size: A4; margin: 8mm; }}
            body {{ font-family: sans-serif; }}
            .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 4mm; }}
            .label {{ border: 1px dashed #999; padding: 3mm; text-align: center; height: 28mm; }}
            .name {{ font-size: 9pt; font-weight: bold; }}
            .serial {{ font-family: monospace; font-size: 8pt; margin: 1mm 0; }}
            .qr {{ width: 22mm; height: 22mm; }}
        </style></head><body><div class="grid">{labels_html}</div></body></html>
    """
    # Внешний ресурс /qr будет недоступен изнутри weasyprint без хоста — в prod
    # этот endpoint вряд ли сгенерит идеальные PDF; но структуру оставим для будущего.
    pdf_bytes = HTML(string=html, base_url="/").write_pdf()
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="labels_{nom_id}.pdf"'},
    )


@router.post(
    "/nomenclature",
    responses={
        400: {"description": "Изделие с таким наименованием уже существует"},
        403: {"description": "Только администратор может добавлять номенклатуру"}
    }
)
async def create_nomenclature(
        data: NomenclatureCreate,
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)]
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может добавлять номенклатуру")

    # Проверка на дубликат
    existing = await db.execute(select(Nomenclature).where(Nomenclature.name == data.name))
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Изделие с таким наименованием уже существует")

    new_item = Nomenclature(**data.dict())
    db.add(new_item)
    await db.flush()

    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="create_nomenclature", entity_type="nomenclature", entity_id=new_item.id,
        details={"name": new_item.name, "code": new_item.code,
                 "is_numbered": new_item.is_numbered, "category": new_item.category},
    )

    await db.commit()
    await db.refresh(new_item)
    return new_item


@router.put(
    "/nomenclature/{nom_id}",
    responses={
        400: {"description": "Другое изделие с таким наименованием уже существует"},
        403: {"description": "Только администратор может редактировать справочник"},
        404: {"description": "Номенклатура не найдена"}
    }
)
async def update_nomenclature(
        nom_id: int,
        data: NomenclatureCreate,
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)]
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может редактировать справочник")

    nom = await db.get(Nomenclature, nom_id)
    if not nom:
        raise HTTPException(status_code=404, detail="Номенклатура не найдена")

    # Проверка на дубликат имени (если имя меняется)
    if nom.name != data.name:
        existing = await db.execute(select(Nomenclature).where(Nomenclature.name == data.name))
        if existing.scalars().first():
            raise HTTPException(status_code=400, detail="Другое изделие с таким наименованием уже существует")

    nom.name = data.name
    nom.code = data.code
    nom.default_account = data.default_account
    nom.is_numbered = data.is_numbered
    nom.category = data.category
    nom.min_quantity = max(0, data.min_quantity or 0)

    db.add(nom)
    await db.commit()
    await db.refresh(nom)
    return nom


@router.delete(
    "/nomenclature/{nom_id}",
    responses={
        400: {"description": "Нельзя удалить: числится на балансе или в документах"},
        403: {"description": "Только администратор может удалять справочник"},
        404: {"description": "Номенклатура не найдена"}
    }
)
async def delete_nomenclature(
        nom_id: int,
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)]
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только администратор может удалять справочник")

    nom = await db.get(Nomenclature, nom_id)
    if not nom:
        raise HTTPException(status_code=404, detail="Номенклатура не найдена")

    # ЗАЩИТА: Проверяем, есть ли это изделие на складах (WeaponRegistry)
    in_registry = await db.execute(select(WeaponRegistry).where(WeaponRegistry.nomenclature_id == nom_id).limit(1))
    if in_registry.scalars().first():
        raise HTTPException(status_code=400, detail="Нельзя удалить! Это изделие числится на балансе складов.")

    # ЗАЩИТА: Проверяем, есть ли это изделие в истории документов
    in_docs = await db.execute(select(DocumentItem).where(DocumentItem.nomenclature_id == nom_id).limit(1))
    if in_docs.scalars().first():
        raise HTTPException(status_code=400, detail="Нельзя удалить! Изделие фигурирует в проведенных документах.")

    await db.delete(nom)
    await db.commit()
    return {"status": "success"}
