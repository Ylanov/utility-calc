# app/modules/utility/routers/admin_periods.py

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from fastapi_cache.decorator import cache
from fastapi_cache import FastAPICache

from app.core.database import get_db, AsyncSessionLocal
from app.modules.utility.models import User, BillingPeriod
from app.modules.utility.schemas import PeriodCreate, PeriodResponse
from app.core.dependencies import get_current_user
from app.modules.utility.services.billing import open_new_period
from app.modules.utility.tasks import close_period_task
from app.modules.utility.services.notification_service import send_push_to_all

router = APIRouter(tags=["Admin Periods"])
logger = logging.getLogger(__name__)


async def _send_period_push(period_name: str):
    """
    Фоновая задача отправки пуш-уведомлений при открытии периода.
    Использует свою собственную сессию БД — не зависит от сессии запроса.

    ИСПРАВЛЕНИЕ: ранее использовался asyncio.create_task() с сессией из запроса.
    Сессия закрывалась до окончания задачи → ошибка Session is closed.
    Теперь создаётся отдельная сессия внутри фоновой задачи.
    """
    async with AsyncSessionLocal() as db:
        try:
            await send_push_to_all(
                db,
                title="📢 Открыт прием показаний!",
                body=f"Начался расчётный период: {period_name}. Пожалуйста, передайте показания счётчиков в приложении."
            )
        except Exception as e:
            logger.error(f"Ошибка отправки пуш-уведомлений при открытии периода: {e}", exc_info=True)


@router.post("/api/admin/periods/close", summary="Закрыть текущий месяц (Фоновая задача)")
async def api_close_period(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    task = close_period_task.delay(current_user.id)

    return {
        "status": "processing",
        "task_id": task.id,
        "message": "Процесс закрытия периода запущен в фоне."
    }


@router.post("/api/admin/periods/open", summary="Открыть новый месяц")
async def api_open_period(
        data: PeriodCreate,
        background_tasks: BackgroundTasks,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    try:
        new_period = await open_new_period(db=db, new_name=data.name)
        await db.commit()
        await FastAPICache.clear(namespace="periods")

        # ИСПРАВЛЕНИЕ: заменён asyncio.create_task() на BackgroundTasks.
        # BackgroundTasks гарантирует выполнение задачи после отправки ответа клиенту,
        # не отменяет её и не разделяет сессию БД с запросом.
        background_tasks.add_task(_send_period_push, new_period.name)

        return {"status": "opened", "period": new_period.name}

    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        await db.rollback()
        # ИСПРАВЛЕНИЕ: print → logger.error, детали ошибки не утекают клиенту.
        logger.error(f"Critical error in api_open_period: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при открытии периода. Обратитесь к администратору."
        )


@router.get("/api/admin/periods/active", response_model=Optional[PeriodResponse])
@cache(expire=300, namespace="periods")
async def get_active_period(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))
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