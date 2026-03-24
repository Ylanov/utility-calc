# app/modules/utility/routers/admin_adjustments.py
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

    # 1. Находим жильца, чтобы узнать его КОМНАТУ
    target_user = await db.get(User, data.user_id)
    if not target_user or not target_user.room_id:
        raise HTTPException(status_code=400, detail="Жилец не найден или не привязан к комнате")

    # 2. Находим активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))
    active_period = res_period.scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода для внесения корректировок")

    # 3. Создаем запись корректировки (для истории)
    adj = Adjustment(
        user_id=data.user_id,
        period_id=active_period.id,
        amount=data.amount,
        description=data.description,
        account_type=data.account_type  # '209' или '205'
    )
    db.add(adj)

    # 4. Обновляем итоговые цифры в показаниях КОМНАТЫ
    amount_209 = data.amount if data.account_type == "209" else Decimal("0.00")
    amount_205 = data.amount if data.account_type == "205" else Decimal("0.00")

    update_stmt = (
        update(MeterReading)
        .where(
            MeterReading.room_id == target_user.room_id, # 🔥 ИСПРАВЛЕНИЕ: Ищем черновик по комнате
            MeterReading.period_id == active_period.id
        )
        .values(
            total_cost=MeterReading.total_cost + data.amount,
            total_209=MeterReading.total_209 + amount_209,
            total_205=MeterReading.total_205 + amount_205
        )
    )

    await db.execute(update_stmt)
    await db.commit()
    await db.refresh(adj)

    return adj


@router.get("/api/admin/adjustments/{user_id}", response_model=list[AdjustmentResponse])
async def get_user_adjustments(
        user_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """Получает список всех корректировок пользователя в ТЕКУЩЕМ АКТИВНОМ периоде."""
    allowed_roles =["accountant", "financier", "admin"]
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))
    active_period = res_period.scalars().first()

    if not active_period:
        return
