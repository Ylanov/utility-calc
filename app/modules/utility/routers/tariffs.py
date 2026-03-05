from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi_cache.decorator import cache
from fastapi_cache import FastAPICache

from app.core.database import get_db
from app.modules.utility.models import User, Tariff
from app.modules.utility.schemas import TariffSchema
from app.core.dependencies import get_current_user, RoleChecker

router = APIRouter(prefix="/api/tariffs", tags=["Tariffs"])
allow_accountant_or_admin = RoleChecker(["accountant", "admin"])


@router.get("", response_model=List[TariffSchema])
@cache(expire=3600, namespace="tariffs")
async def get_tariffs(
        current_user: User = Depends(get_current_user),
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
        current_user: User = Depends(allow_accountant_or_admin),
        db: AsyncSession = Depends(get_db)
):
    """
    Создать новый тарифный профиль или обновить существующий.
    Если в `data` передан `id`, происходит обновление.
    Если `id` отсутствует, создается новая запись.
    """
    if data.id:
        # Режим обновления
        result = await db.execute(select(Tariff).where(Tariff.id == data.id))
        tariff = result.scalars().first()
        if not tariff:
            raise HTTPException(status_code=404, detail="Тарифный профиль не найден")
    else:
        # Режим создания
        tariff = Tariff()
        db.add(tariff)

    # Обновляем все поля из пришедшей схемы, кроме ID
    for key, value in data.dict(exclude={"id"}).items():
        setattr(tariff, key, value)

    await db.commit()
    await db.refresh(tariff)

    # Сбрасываем кэш, так как данные изменились
    await FastAPICache.clear(namespace="tariffs")

    return tariff


@router.delete("/{tariff_id}", status_code=204)
async def delete_tariff(
        tariff_id: int,
        current_user: User = Depends(allow_accountant_or_admin),
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

    result = await db.execute(select(Tariff).where(Tariff.id == tariff_id))
    tariff = result.scalars().first()

    if not tariff:
        raise HTTPException(status_code=404, detail="Тарифный профиль не найден")

    if tariff.is_active:
        tariff.is_active = False  # Soft delete
        await db.commit()
        await FastAPICache.clear(namespace="tariffs")

    # Возвращаем пустой ответ со статусом 204 No Content, как принято для DELETE операций
    return None