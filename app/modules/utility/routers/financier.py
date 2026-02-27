import os
import uuid
import shutil
import logging
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, BillingPeriod
from app.core.dependencies import get_current_user
from app.modules.utility.schemas import PaginatedResponse, UserDebtResponse
from app.modules.utility.tasks import import_debts_task

router = APIRouter(prefix="/api/financier", tags=["Financier"])
logger = logging.getLogger(__name__)

TEMP_DIR = "/app/static/temp_imports"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
os.makedirs(TEMP_DIR, exist_ok=True)

@router.post("/import-debts", summary="Фоновый импорт долгов из 1С")
async def upload_debts_1c(
    account_type: str = Form(..., pattern="^(209|205)$", description="Тип счета: 209 или 205"),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in ("financier", "accountant"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Поддерживаются только Excel-файлы")

    header = await file.read(8)
    await file.seek(0)
    if not (header.startswith(b"PK\x03\x04") or header.startswith(b"\xd0\xcf\x11\xe0")):
        raise HTTPException(status_code=400, detail="Вредоносный файл или поддельное расширение!")

    file.file.seek(0, 2)
    file_size = file.file.tell()
    await file.seek(0)

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"Файл слишком большой. Максимум {MAX_FILE_SIZE / 1024 / 1024} MB")

    ext = file.filename.split(".")[-1]
    unique_name = f"{uuid.uuid4()}.{ext}"
    file_path = os.path.join(TEMP_DIR, unique_name)

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception:
        logger.exception("Failed to save uploaded file")
        raise HTTPException(status_code=500, detail="Ошибка сохранения файла")

    # Передаем account_type в задачу
    task = import_debts_task.delay(file_path, account_type)
    logger.info(f"[IMPORT] Started task={task.id} for account={account_type}")

    return {
        "task_id": task.id,
        "status": "processing",
        "account_type": account_type
    }

@router.get("/users-status", response_model=PaginatedResponse[UserDebtResponse], summary="Список пользователей с долгами")
async def get_users_with_debts(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=500),
    search: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("financier", "accountant"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    offset = (page - 1) * limit

    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active.is_(True)))
    active_period = res_period.scalars().first()
    period_id = active_period.id if active_period else None

    stmt = select(
        User.id,
        User.username,
        User.dormitory,
        MeterReading.debt_209,
        MeterReading.overpayment_209,
        MeterReading.debt_205,
        MeterReading.overpayment_205,
        MeterReading.total_cost
    ).outerjoin(
        MeterReading,
        (User.id == MeterReading.user_id) & (MeterReading.period_id == period_id)
    )

    if search:
        search_value = f"%{search.lower()}%"
        stmt = stmt.where(func.lower(User.username).like(search_value))

    count_stmt = select(func.count(User.id))
    if search:
        count_stmt = count_stmt.where(func.lower(User.username).like(search_value))

    total_res = await db.execute(count_stmt)
    total_items = total_res.scalar_one()

    stmt = stmt.order_by(User.dormitory, User.username).limit(limit).offset(offset)
    result = await db.execute(stmt)
    rows = result.all()

    items = []
    for row in rows:
        items.append({
            "id": row.id,
            "username": row.username,
            "dormitory": row.dormitory,
            "debt_209": row.debt_209 or 0,
            "overpayment_209": row.overpayment_209 or 0,
            "debt_205": row.debt_205 or 0,
            "overpayment_205": row.overpayment_205 or 0,
            "current_total_cost": row.total_cost or 0
        })

    return {
        "total": total_items,
        "page": page,
        "size": limit,
        "items": items
    }