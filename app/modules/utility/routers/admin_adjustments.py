# app/modules/utility/routers/admin_adjustments.py

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update
from decimal import Decimal

from app.core.database import get_db
from app.modules.utility.models import User, Adjustment, BillingPeriod, MeterReading
from app.modules.utility.schemas import AdjustmentCreate, AdjustmentResponse
from app.core.dependencies import get_current_user

router = APIRouter(tags=["Admin Adjustments"])
logger = logging.getLogger(__name__)


@router.post("/api/admin/adjustments", response_model=AdjustmentResponse)
async def create_adjustment(
        data: AdjustmentCreate,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Создает финансовую корректировку (перерасчет) для пользователя в текущем активном периоде.
    Поддерживает раздельный учет по счетам 209 и 205.
    """
    allowed_roles = ["accountant", "financier", "admin"]
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # 1. Находим жильца
    target_user = await db.get(User, data.user_id)

    # ИСПРАВЛЕНИЕ: добавлена проверка is_deleted.
    # Ранее корректировку можно было создать для мягко удалённого пользователя —
    # он попал бы в отчёты как живой должник.
    if not target_user or target_user.is_deleted:
        raise HTTPException(status_code=404, detail="Жилец не найден или удалён из системы")

    if not target_user.room_id:
        raise HTTPException(status_code=400, detail="Жилец не привязан к комнате")

    # 2. Находим активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))
    active_period = res_period.scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода для внесения корректировок")

    # 3. Создаем запись корректировки
    adj = Adjustment(
        user_id=data.user_id,
        period_id=active_period.id,
        amount=data.amount,
        description=data.description,
        account_type=data.account_type  # '209' или '205'
    )
    db.add(adj)

    # 4. Обновляем черновик показания если он уже есть — пересчитываем итог
    draft = (await db.execute(
        select(MeterReading).where(
            MeterReading.room_id == target_user.room_id,
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(False)
        )
    )).scalars().first()

    if draft:
        amount = Decimal(str(data.amount))
        if data.account_type == "209":
            draft.total_209 = (draft.total_209 or Decimal("0.00")) + amount
        elif data.account_type == "205":
            draft.total_205 = (draft.total_205 or Decimal("0.00")) + amount
        draft.total_cost = (draft.total_209 or Decimal("0.00")) + (draft.total_205 or Decimal("0.00"))
        db.add(draft)

    await db.commit()
    await db.refresh(adj)

    logger.info(
        f"Adjustment created: user_id={data.user_id}, amount={data.amount}, "
        f"account={data.account_type}, by={current_user.username}"
    )

    return adj


@router.get("/api/admin/adjustments/{user_id}", response_model=list[AdjustmentResponse])
async def get_user_adjustments(
        user_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """Список всех корректировок конкретного жильца."""
    allowed_roles = ["accountant", "financier", "admin"]
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    result = await db.execute(
        select(Adjustment)
        .where(Adjustment.user_id == user_id)
        .order_by(Adjustment.created_at.desc())
    )
    return result.scalars().all()


@router.delete("/api/admin/adjustments/{adjustment_id}")
async def delete_adjustment(
        adjustment_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Удаление корректировки с обратным пересчётом черновика.

    ИСПРАВЛЕНИЕ: ранее при удалении корректировки черновик показания
    оставался с завышенной/заниженной суммой, т.к. при создании корректировки
    сумма прибавлялась к draft.total_209/205, но при удалении — не вычиталась.
    Теперь сумма корректно откатывается.
    """
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    adj = await db.get(Adjustment, adjustment_id)
    if not adj:
        raise HTTPException(status_code=404, detail="Корректировка не найдена")

    # --- ИСПРАВЛЕНИЕ: откатываем сумму из черновика, если он существует ---
    target_user = await db.get(User, adj.user_id)
    if target_user and target_user.room_id:
        draft = (await db.execute(
            select(MeterReading).where(
                MeterReading.room_id == target_user.room_id,
                MeterReading.period_id == adj.period_id,
                MeterReading.is_approved.is_(False)
            )
        )).scalars().first()

        if draft:
            amount = Decimal(str(adj.amount))
            if adj.account_type == "209":
                draft.total_209 = (draft.total_209 or Decimal("0.00")) - amount
            elif adj.account_type == "205":
                draft.total_205 = (draft.total_205 or Decimal("0.00")) - amount
            draft.total_cost = (draft.total_209 or Decimal("0.00")) + (draft.total_205 or Decimal("0.00"))
            db.add(draft)

    await db.delete(adj)
    await db.commit()

    logger.info(f"Adjustment {adjustment_id} deleted (with draft rollback) by {current_user.username}")
    return {"status": "deleted"}