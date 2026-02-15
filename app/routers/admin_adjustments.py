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
    Использует блокировку строки для предотвращения Race Condition при обновлении total_cost.
    """
    # Проверка ролей
    allowed_roles = ["accountant", "financier"]
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # 1. Находим активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода для внесения корректировок")

    # Начинаем явную транзакцию
    async with db.begin():
        # 2. Создаем запись корректировки
        adj = Adjustment(
            user_id=data.user_id,
            period_id=active_period.id,
            amount=data.amount,
            description=data.description
        )
        db.add(adj)
        # Получаем ID корректировки (flush отправляет запрос в БД, но не коммитит)
        await db.flush()

        # 3. ОБНОВЛЕНИЕ ЧЕРНОВИКА (METER_READING) ДЛЯ UI
        # with_for_update() блокирует строку чтения, чтобы никто другой не мог её изменить
        # параллельно (предотвращает Race Condition при сложении денег)
        res_reading = await db.execute(
            select(MeterReading)
            .where(
                MeterReading.user_id == data.user_id,
                MeterReading.period_id == active_period.id
            )
            .with_for_update()
        )
        reading = res_reading.scalars().first()

        if reading:
            # Обновляем total_cost для визуального отображения текущего итога.
            # Важно: Полный и окончательный пересчет стоимости с учетом всех корректировок
            # гарантируется в методе approve_reading, который является "источником истины".
            current_total = reading.total_cost if reading.total_cost is not None else Decimal("0.00")
            reading.total_cost = current_total + data.amount

        # Транзакция закоммитится автоматически при выходе из блока async with

    # Обновляем объект из БД, чтобы вернуть актуальные данные (например, ID)
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
    allowed_roles = ["accountant", "financier"]
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()

    if not active_period:
        return []

    res = await db.execute(
        select(Adjustment)
        .where(Adjustment.user_id == user_id, Adjustment.period_id == active_period.id)
    )
    return res.scalars().all()