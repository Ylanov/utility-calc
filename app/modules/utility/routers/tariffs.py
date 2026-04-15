# app/modules/utility/routers/tariffs.py

import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, update, text
from fastapi_cache.decorator import cache
from fastapi_cache import FastAPICache

from app.core.database import get_db
from app.modules.utility.models import User, Tariff
from app.modules.utility.schemas import TariffSchema
from app.core.dependencies import get_current_user, RoleChecker

# ИМПОРТ ДЛЯ ЖУРНАЛА ДЕЙСТВИЙ
from app.modules.utility.routers.admin_dashboard import write_audit_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tariffs", tags=["Tariffs"])
allow_management_roles = RoleChecker(["accountant", "admin", "financier"])


# =====================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: Безопасная очистка кэша
# =====================================================
async def _safe_clear_cache(namespace: str = "tariffs"):
    """
    Очищает кэш без выброса исключения.
    Если Redis/кэш-бэкенд недоступен — логируем и продолжаем.
    КРИТИЧЕСКИ ВАЖНО: ранее падение кэша при clear() вызывало 500,
    хотя данные в БД уже были успешно сохранены.
    """
    try:
        await FastAPICache.clear(namespace=namespace)
    except Exception as e:
        logger.warning(f"Не удалось очистить кэш '{namespace}': {e}")


# =====================================================
# GET /api/tariffs — Список активных тарифов (кэшированный)
# =====================================================
@router.get("", response_model=List[TariffSchema])
@cache(expire=3600, namespace="tariffs")
async def get_tariffs(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """Получить список всех активных тарифов, отсортированных по ID."""
    result = await db.execute(
        select(Tariff).where(Tariff.is_active).order_by(Tariff.id)
    )
    return result.scalars().all()


# =====================================================
# GET /api/tariffs/with-stats — Тарифы + кол-во жильцов
# =====================================================
@router.get("/with-stats", summary="Тарифы с количеством привязанных жильцов")
async def get_tariffs_with_stats(
        current_user: User = Depends(allow_management_roles),
        db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список активных тарифов с количеством привязанных пользователей.
    Позволяет администратору видеть сколько жильцов используют каждый тариф,
    что критично перед удалением или изменением.
    """
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


# =====================================================
# POST /api/tariffs — Создание / обновление тарифа
# =====================================================
@router.post("", response_model=TariffSchema)
async def create_or_update_tariff(
        data: TariffSchema,
        current_user: User = Depends(allow_management_roles),
        db: AsyncSession = Depends(get_db)
):
    """
    Создать новый тарифный профиль или обновить существующий.
    Если в `data` передан `id`, происходит обновление.
    Если `id` отсутствует, создается новая запись.

    ИСПРАВЛЕНИЕ: Добавлен try/except + rollback. И синхронизация Sequence.
    """
    try:
        if data.id:
            # Режим обновления
            tariff = await db.get(Tariff, data.id)
            if not tariff:
                raise HTTPException(status_code=404, detail="Тарифный профиль не найден")

            if not tariff.is_active:
                raise HTTPException(
                    status_code=400,
                    detail="Нельзя редактировать деактивированный тариф. Создайте новый."
                )
        else:
            # Синхронизируем счетчик БД перед созданием,
            # чтобы избежать UniqueViolationError при первой записи через API.
            try:
                await db.execute(text("SELECT setval('tariffs_id_seq', COALESCE((SELECT MAX(id) FROM tariffs), 1))"))
            except Exception as seq_err:
                logger.warning(f"Не удалось обновить секвенцию тарифов: {seq_err}")

            # Режим создания
            tariff = Tariff()
            db.add(tariff)

        # Обновляем все поля из пришедшей схемы, кроме ID и системных
        # Поддержка разных версий Pydantic
        if hasattr(data, "model_dump"):
            update_data = data.model_dump(exclude={"id", "is_active"})
        else:
            update_data = data.dict(exclude={"id", "is_active"})

        for key, value in update_data.items():
            if hasattr(tariff, key):
                setattr(tariff, key, value)

        # ЗАПИСЬ В ЖУРНАЛ: Создание/Обновление тарифа
        action_type = "update" if data.id else "create"
        await write_audit_log(
            db, current_user.id, current_user.username,
            action=action_type, entity_type="tariff", entity_id=tariff.id,
            details={"tariff_name": tariff.name}
        )

        await db.commit()
        await db.refresh(tariff)

    except HTTPException:
        # Штатные HTTP-ошибки (404, 400) пробрасываем как есть
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Ошибка при сохранении тарифа: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при сохранении тарифа. Обратитесь к администратору."
        )

    # Сбрасываем кэш ПОСЛЕ успешного коммита, безопасно.
    await _safe_clear_cache("tariffs")

    return tariff


# =====================================================
# DELETE /api/tariffs/{tariff_id} — Мягкое удаление
# =====================================================
@router.delete("/{tariff_id}", status_code=204)
async def delete_tariff(
        tariff_id: int,
        current_user: User = Depends(allow_management_roles),
        db: AsyncSession = Depends(get_db)
):
    """
    Мягкое удаление тарифа (пометка неактивным) с пересадкой жильцов на базовый.

    При удалении тарифа все привязанные к нему жильцы автоматически переводятся
    на базовый тариф (tariff_id = NULL → fallback id=1).
    """
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

    try:
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
            await db.execute(
                update(User)
                .where(User.tariff_id == tariff_id, User.is_deleted.is_(False))
                .values(tariff_id=None)
            )

        # Мягкое удаление
        tariff.is_active = False

        # ЗАПИСЬ В ЖУРНАЛ: Удаление тарифа
        await write_audit_log(
            db, current_user.id, current_user.username,
            action="delete", entity_type="tariff", entity_id=tariff_id,
            details={"tariff_name": tariff.name, "users_reassigned": affected_count}
        )

        await db.commit()

    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Ошибка при удалении тарифа {tariff_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при удалении тарифа. Обратитесь к администратору."
        )

    # Безопасная очистка кэша после удаления
    await _safe_clear_cache("tariffs")

    return None