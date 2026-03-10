from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi_cache.decorator import cache
from fastapi_cache import FastAPICache

from app.core.database import get_db
from app.modules.utility.models import User, Tariff
from app.modules.utility.schemas import TariffSchema
# ИСПРАВЛЕНИЕ: Добавляем финансиста в список тех, кто может управлять тарифами
from app.core.dependencies import get_current_user, RoleChecker

router = APIRouter(prefix="/api/tariffs", tags=["Tariffs"])
# Добавляем роль 'financier', так как это тоже управляющая роль
allow_management_roles = RoleChecker(["accountant", "admin", "financier"])


@router.get("", response_model=List[TariffSchema])
@cache(expire=3600, namespace="tariffs")  # Кэшируем на 1 час
async def get_tariffs(
        current_user: User = Depends(get_current_user),  # Доступно всем для чтения
        db: AsyncSession = Depends(get_db)
):
    """Получить список всех активных тарифов, отсортированных по ID."""
    result = await db.execute(
        select(Tariff).where(Tariff.is_active == True).order_by(Tariff.id)
    )
    return result.scalars().all()


@router.post("", response_model=TariffSchema)
async def create_or_update_tariff(
        data: TariffSchema,
        current_user: User = Depends(allow_management_roles),  # Права на запись
        db: AsyncSession = Depends(get_db)
):
    """
    Создать новый тарифный профиль или обновить существующий.
    Если в `data` передан `id`, происходит обновление.
    Если `id` отсутствует, создается новая запись.
    """
    if data.id:
        # Режим обновления
        tariff = await db.get(Tariff, data.id)
        if not tariff:
            raise HTTPException(status_code=404, detail="Тарифный профиль не найден")
    else:
        # Режим создания
        tariff = Tariff()
        db.add(tariff)

    # Обновляем все поля из пришедшей схемы, кроме ID и системных
    update_data = data.dict(exclude={"id", "is_active"})
    for key, value in update_data.items():
        if hasattr(tariff, key):  # Доп. проверка на существование атрибута
            setattr(tariff, key, value)

    await db.commit()
    await db.refresh(tariff)

    # ВАЖНО: Сбрасываем кэш, так как данные изменились
    await FastAPICache.clear(namespace="tariffs")

    return tariff


@router.delete("/{tariff_id}", status_code=204)
async def delete_tariff(
        tariff_id: int,
        current_user: User = Depends(allow_management_roles),  # Права на удаление
        db: AsyncSession = Depends(get_db)
):
    """
    Мягкое удаление тарифа (пометка неактивным).
    Это позволяет сохранить историческую целостность для старых квитанций.
    """
    # Защита от удаления базового тарифа, который является фоллбэком
    if tariff_id == 1:
        raise HTTPException(
            status_code=400,
            detail="Нельзя удалить базовый тарифный профиль (ID=1)"
        )

    tariff = await db.get(Tariff, tariff_id)

    if not tariff:
        raise HTTPException(status_code=404, detail="Тарифный профиль не найден")

    if tariff.is_active:
        tariff.is_active = False  # Soft delete
        await db.commit()

        # ВАЖНО: Сбрасываем кэш после удаления
        await FastAPICache.clear(namespace="tariffs")

    # Возвращаем пустой ответ со статусом 204 No Content
    return None