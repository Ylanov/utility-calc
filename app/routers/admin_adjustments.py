from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from decimal import Decimal

from app.database import get_db
from app.models import User, Adjustment, BillingPeriod, MeterReading
from app.schemas import AdjustmentCreate, AdjustmentResponse
from app.dependencies import get_current_user

router = APIRouter(tags=["Admin Adjustments"])


@router.post("/api/admin/adjustments", response_model=AdjustmentResponse)
async def create_adjustment(
        data: AdjustmentCreate,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Создает финансовую корректировку (перерасчет) для пользователя в текущем активном периоде.
    Автоматически обновляет поле total_cost в текущих показаниях пользователя.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # 1. Находим активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода для внесения корректировок")

    # 2. Создаем запись корректировки
    adj = Adjustment(
        user_id=data.user_id,
        period_id=active_period.id,
        amount=data.amount,
        description=data.description
    )
    db.add(adj)

    # 3. АВТОМАТИЧЕСКИЙ ПЕРЕСЧЕТ ИТОГА В METER_READING
    # Находим текущее показание (черновик или уже утвержденное)
    res_reading = await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == data.user_id,
            MeterReading.period_id == active_period.id
        )
    )
    reading = res_reading.scalars().first()

    if reading:
        # Если запись с показаниями уже есть, обновляем total_cost
        # ВАЖНО: Мы просто добавляем сумму корректировки к текущему итогу.
        # Если amount отрицательный (скидка), итог уменьшится.
        current_total = reading.total_cost if reading.total_cost is not None else Decimal("0.00")
        reading.total_cost = current_total + data.amount

    # Сохраняем изменения
    await db.commit()
    await db.refresh(adj)

    return adj


@router.get("/api/admin/adjustments/{user_id}", response_model=list[AdjustmentResponse])
async def get_user_adjustments(
        user_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Получает список всех корректировок пользователя в ТЕКУЩЕМ АКТИВНОМ периоде.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # Находим активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()

    if not active_period:
        return []

    res = await db.execute(
        select(Adjustment)
        .where(Adjustment.user_id == user_id, Adjustment.period_id == active_period.id)
    )
    return res.scalars().all()