from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc

from app.database import get_db
from app.models import User, BillingPeriod
from app.schemas import PeriodCreate, PeriodResponse
from app.dependencies import get_current_user
from app.services.billing import close_current_period, open_new_period

router = APIRouter(tags=["Admin Periods"])


@router.post("/api/admin/periods/close", summary="Закрыть текущий месяц")
async def api_close_period(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    try:
        # --- ИЗМЕНЕНИЕ: Переход на ручное управление транзакцией ---
        # Вызываем сервис, который готовит все изменения
        result = await close_current_period(db=db, admin_user_id=current_user.id)

        # Если все прошло без ошибок, явно коммитим изменения
        await db.commit()

        return result
    except ValueError as e:
        # Если сервис вернул ошибку, откатываем все изменения
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        # При любой другой ошибке тоже откатываем
        await db.rollback()
        print(f"!!! Critical Error in api_close_period: {e}")
        raise HTTPException(status_code=500, detail=f"Внутренняя ошибка сервера: {e}")


@router.post("/api/admin/periods/open", summary="Открыть новый месяц")
async def api_open_period(
        data: PeriodCreate,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    try:
        # --- И ЗДЕСЬ ТОЖЕ ПЕРЕХОДИМ НА РУЧНОЕ УПРАВЛЕНИЕ ---
        new_period = await open_new_period(db=db, new_name=data.name)

        # Коммитим создание нового периода
        await db.commit()

        return {"status": "opened", "period": new_period.name}
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        await db.rollback()
        print(f"!!! Critical Error in api_open_period: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/admin/periods/active", response_model=Optional[PeriodResponse], summary="Текущий активный месяц")
async def get_active_period(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    return res.scalars().first()


@router.get("/api/admin/periods/history", response_model=List[PeriodResponse], summary="История всех периодов")
async def get_all_periods(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    res = await db.execute(select(BillingPeriod).order_by(desc(BillingPeriod.id)))
    return res.scalars().all()