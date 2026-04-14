# app/modules/utility/routers/tariffs.py

from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
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
        select(Tariff).where(Tariff.is_active).order_by(Tariff.id)
    )
    return result.scalars().all()


@router.get("/with-stats", summary="Тарифы с количеством привязанных жильцов")
async def get_tariffs_with_stats(
        current_user: User = Depends(allow_management_roles),
        db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список активных тарифов с количеством привязанных пользователей.

    НОВАЯ ФУНКЦИЯ: Позволяет администратору видеть сколько жильцов
    используют каждый тариф, что критично перед удалением или изменением.
    """
    # Получаем тарифы
    tariffs_result = await db.execute(
        select(Tariff).where(Tariff.is_active).order_by(Tariff.id)
    )
    tariffs = tariffs_result.scalars().all()

    # Считаем пользователей по каждому тарифу одним запросом
    user_counts_result = await db.execute(
        select(
            User.tariff_id,
            func.count(User.id).label("user_count")
        )
        .where(User.is_deleted.is_(False))
        .group_by(User.tariff_id)
    )
    user_counts_map = {row[0]: row[1] for row in user_counts_result.all()}

    # Считаем пользователей без тарифа (tariff_id IS NULL) — они на дефолтном
    null_count = user_counts_map.get(None, 0)

    result = []
    for t in tariffs:
        direct_count = user_counts_map.get(t.id, 0)
        # Если это базовый тариф (id=1), добавляем пользователей без привязки
        effective_count = direct_count + (null_count if t.id == 1 else 0)

        result.append({
            "id": t.id,
            "name": t.name,
            "is_active": t.is_active,
            "user_count": effective_count,
            "maintenance_repair": t.maintenance_repair,
            "social_rent": t.social_rent,
            "heating": t.heating,
            "water_heating": t.water_heating,
            "water_supply": t.water_supply,
            "sewage": t.sewage,
            "waste_disposal": t.waste_disposal,
            "electricity_per_sqm": t.electricity_per_sqm,
            "electricity_rate": t.electricity_rate,
        })

    return result


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

        # ИСПРАВЛЕНИЕ: Нельзя редактировать деактивированный (мягко удалённый) тариф.
        if not tariff.is_active:
            raise HTTPException(
                status_code=400,
                detail="Нельзя редактировать деактивированный тариф. Создайте новый."
            )
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
    Мягкое удаление тарифа (пометка неактивным) с пересадкой жильцов на базовый.

    ИСПРАВЛЕНИЕ: Ранее при удалении тарифа пользователи оставались с tariff_id,
    указывающим на деактивированный тариф. При расчёте система тихо падала на
    fallback к default_tariff через getattr(user, 'tariff_id', 1), но администратор
    не знал об этом.

    Теперь: при мягком удалении тарифа все привязанные к нему жильцы
    автоматически переводятся на базовый тариф (tariff_id = NULL → fallback id=1).
    Администратор видит в ответе сколько жильцов было пересажено.
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

    if not tariff.is_active:
        # Уже удалён — идемпотентность
        return None

    # Считаем сколько жильцов на этом тарифе
    affected_count_result = await db.execute(
        select(func.count(User.id)).where(
            User.tariff_id == tariff_id,
            User.is_deleted.is_(False)
        )
    )
    affected_count = affected_count_result.scalar_one()

    # Пересаживаем жильцов на базовый тариф (NULL = fallback на id=1)
    if affected_count > 0:
        from sqlalchemy import update
        await db.execute(
            update(User)
            .where(User.tariff_id == tariff_id, User.is_deleted.is_(False))
            .values(tariff_id=None)
        )

    # Мягкое удаление
    tariff.is_active = False
    await db.commit()

    # ВАЖНО: Сбрасываем кэш после удаления
    await FastAPICache.clear(namespace="tariffs")

    # Возвращаем 204 No Content (стандарт REST для DELETE)
    return None