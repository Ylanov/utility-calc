from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

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
        # Сервис billing.py уже обновлен для работы с Decimal
        result = await close_current_period(db=db, admin_user_id=current_user.id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error closing period: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/admin/periods/open", summary="Открыть новый месяц")
async def api_open_period(
        data: PeriodCreate,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    try:
        new_period = await open_new_period(db=db, new_name=data.name)
        return {"status": "opened", "period": new_period.name}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        print(f"Error opening period: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/admin/periods/active", response_model=Optional[PeriodResponse], summary="Текущий активный месяц")
async def get_active_period(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    return res.scalars().first()