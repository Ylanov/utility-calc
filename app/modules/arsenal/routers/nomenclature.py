from typing import Optional, Annotated
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_

from app.core.database import get_arsenal_db
from app.modules.arsenal.models import Nomenclature, ArsenalUser, WeaponRegistry, DocumentItem
from app.modules.arsenal.schemas import NomenclatureCreate
from app.modules.arsenal.deps import get_current_arsenal_user

router = APIRouter(tags=["Arsenal Nomenclature"])


@router.get("/nomenclature")
async def get_nomenclature(
        db: Annotated[AsyncSession, Depends(get_arsenal_db)],
        current_user: Annotated[ArsenalUser, Depends(get_current_arsenal_user)],
        skip: Annotated[int, Query(ge=0, description="Смещение")] = 0,
        limit: Annotated[int, Query(ge=1, le=5000, description="Лимит записей")] = 100,
        q: Annotated[Optional[str], Query(min_length=1, description="Поиск по названию или индексу")] = None
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

    # Сортировка и пагинация
    stmt = stmt.order_by(Nomenclature.name).offset(skip).limit(limit)

    result = await db.execute(stmt)
    return result.scalars().all()


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
