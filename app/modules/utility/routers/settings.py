from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.database import get_db
from app.modules.utility.models import User, SystemSetting
from app.core.dependencies import RoleChecker

router = APIRouter(prefix="/api/settings", tags=["System Settings"])

# ИСПРАВЛЕНИЕ: Разрешаем доступ пользователям с ролью accountant или admin.
# Так как ваш главный пользователь в БД имеет роль accountant, теперь его пустит.
allow_accountant_or_admin = RoleChecker(["accountant", "admin"])


class SubmissionPeriodSchema(BaseModel):
    start_day: int
    end_day: int


@router.get("/submission-period", response_model=SubmissionPeriodSchema)
async def get_submission_period(
    current_user: User = Depends(allow_accountant_or_admin),
    db: AsyncSession = Depends(get_db)
):
    """Получить дни подачи показаний."""
    start = await db.get(SystemSetting, "submission_start_day")
    end = await db.get(SystemSetting, "submission_end_day")

    return {
        "start_day": int(start.value) if start else 20,
        "end_day": int(end.value) if end else 25
    }


@router.post("/submission-period")
async def update_submission_period(
    data: SubmissionPeriodSchema,
    current_user: User = Depends(allow_accountant_or_admin), # Применяем проверку прав здесь
    db: AsyncSession = Depends(get_db)
):
    """Обновить дни подачи показаний (Доступно бухгалтеру и админу)."""
    if not (1 <= data.start_day <= 28) or not (1 <= data.end_day <= 28):
        raise HTTPException(status_code=400, detail="Дни должны быть от 1 до 28")

    if data.start_day >= data.end_day:
        raise HTTPException(status_code=400, detail="День начала должен быть раньше дня окончания")

    # Helper для upsert (создания или обновления)
    async def upsert(key, val, desc):
        item = await db.get(SystemSetting, key)
        if item:
            item.value = str(val)
        else:
            db.add(SystemSetting(key=key, value=str(val), description=desc))

    await upsert("submission_start_day", data.start_day, "День начала приема показаний")
    await upsert("submission_end_day", data.end_day, "День окончания приема показаний")

    await db.commit()
    return {"status": "success"}