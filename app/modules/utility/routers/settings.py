from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from app.core.database import get_db
from app.modules.utility.models import User, SystemSetting
from app.core.dependencies import RoleChecker, get_current_user

router = APIRouter(prefix="/api/settings", tags=["System Settings"])

allow_accountant_or_admin = RoleChecker(["accountant", "admin", "financier"])


class SubmissionPeriodSchema(BaseModel):
    start_day: int = Field(..., ge=1, le=28)
    end_day: int = Field(..., ge=1, le=28)


@router.get("/submission-period", response_model=SubmissionPeriodSchema)
async def get_submission_period(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Получить дни подачи показаний.
    Доступно всем авторизованным пользователям.
    """

    start = await db.get(SystemSetting, "submission_start_day")
    end = await db.get(SystemSetting, "submission_end_day")

    def safe_int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    return SubmissionPeriodSchema(
        start_day=safe_int(start.value if start else None, 20),
        end_day=safe_int(end.value if end else None, 25)
    )


@router.post("/submission-period")
async def update_submission_period(
    data: SubmissionPeriodSchema,
    current_user: User = Depends(allow_accountant_or_admin),
    db: AsyncSession = Depends(get_db)
):
    """
    Обновить дни подачи показаний.
    Доступно бухгалтеру, финансисту и админу.
    """

    if data.start_day >= data.end_day:
        raise HTTPException(
            status_code=400,
            detail="День начала должен быть раньше дня окончания"
        )

    async def upsert(key: str, val: int, desc: str):
        item = await db.get(SystemSetting, key)
        if item:
            item.value = str(val)
        else:
            db.add(SystemSetting(
                key=key,
                value=str(val),
                description=desc
            ))

    await upsert("submission_start_day", data.start_day, "День начала приема показаний")
    await upsert("submission_end_day", data.end_day, "День окончания приема показаний")

    await db.commit()

    return {
        "status": "success",
        "message": "График успешно обновлен"
    }
