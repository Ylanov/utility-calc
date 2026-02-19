from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from fastapi_cache.decorator import cache  # <-- Импорт декоратора

from app.database import get_db
from app.models import User, Tariff
from app.schemas import TariffSchema
from app.dependencies import get_current_user

router = APIRouter(prefix="/api/tariffs", tags=["Tariffs"])


@router.get("", response_model=TariffSchema)
@cache(expire=3600)  # <-- Кэшируем ответ на 1 час (3600 сек)
async def get_tariffs(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # Даже если 12000 пользователей обновят страницу одновременно,
    # запрос к БД будет всего один в час.
    result = await db.execute(select(Tariff).where(Tariff.id == 1))
    return result.scalars().first()


@router.post("")
async def update_tariffs(data: TariffSchema, current_user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403)

    result = await db.execute(select(Tariff).where(Tariff.id == 1))
    tariff = result.scalars().first()

    for k, v in data.dict().items():
        setattr(tariff, k, v)

    await db.commit()

    # ВАЖНО: При обновлении тарифов кэш нужно сбросить вручную,
    # либо просто подождать истечения времени (1 час не критично).
    # Для полной правильности можно использовать FastAPICache.clear()
    from fastapi_cache import FastAPICache
    await FastAPICache.clear(namespace="fastapi-cache")

    return tariff