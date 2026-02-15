# app/routers/financier.py

import os
import uuid
import shutil
import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
    File,
    Query
)

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func

from app.database import get_db
from app.models import User, MeterReading, BillingPeriod
from app.dependencies import get_current_user

from app.schemas import (
    PaginatedResponse,
    UserDebtResponse
)

from app.tasks import import_debts_task

# ======================================================
# ROUTER
# ======================================================

router = APIRouter(
    prefix="/api/financier",
    tags=["Financier"]
)

# ======================================================
# LOGGER
# ======================================================

logger = logging.getLogger(__name__)

# ======================================================
# TEMP STORAGE & CONSTANTS
# ======================================================

TEMP_DIR = "/app/static/temp_imports"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB limit

os.makedirs(TEMP_DIR, exist_ok=True)


# ======================================================
# IMPORT DEBTS (BACKGROUND)
# ======================================================

@router.post(
    "/import-debts",
    summary="Фоновый импорт долгов из 1С"
)
async def upload_debts_1c(
        file: UploadFile = File(...),
        current_user: User = Depends(get_current_user)
):
    """
    Загружает Excel-файл и запускает фоновый импорт через Celery.
    Содержит проверку размера файла.
    """

    # --- Access control ---

    if current_user.role not in ("financier", "accountant"):
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещен"
        )

    # --- Validate file extension ---

    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Поддерживаются только Excel-файлы"
        )

    # --- Validate file size ---
    # Перемещаем курсор в конец файла, чтобы узнать размер
    file.file.seek(0, 2)
    file_size = file.file.tell()
    # Возвращаем курсор в начало
    await file.seek(0)

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Файл слишком большой. Максимум {MAX_FILE_SIZE / 1024 / 1024} MB"
        )

    # --- Save file to disk ---

    ext = file.filename.split(".")[-1]
    unique_name = f"{uuid.uuid4()}.{ext}"
    file_path = os.path.join(TEMP_DIR, unique_name)

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

    except Exception as e:
        logger.exception("Failed to save uploaded file")
        raise HTTPException(
            status_code=500,
            detail="Ошибка сохранения файла"
        )

    # --- Run Celery task ---

    task = import_debts_task.delay(file_path)

    logger.info(f"[IMPORT] Started task={task.id}")

    return {
        "task_id": task.id,
        "status": "processing"
    }


# ======================================================
# USERS DEBT STATUS (PAGINATED)
# ======================================================

@router.get(
    "/users-status",
    response_model=PaginatedResponse[UserDebtResponse],
    summary="Список пользователей с долгами"
)
async def get_users_with_debts(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=500),
        search: str | None = Query(None),

        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список пользователей с долгами за активный период
    (с пагинацией и поиском).
    """

    # --- Access control ---

    if current_user.role not in ("financier", "accountant"):
        raise HTTPException(
            status_code=403,
            detail="Доступ запрещен"
        )

    offset = (page - 1) * limit

    # --------------------------------------------------
    # Active period
    # --------------------------------------------------

    res_period = await db.execute(
        select(BillingPeriod)
        .where(BillingPeriod.is_active.is_(True))
    )

    active_period = res_period.scalars().first()
    period_id = active_period.id if active_period else None

    # --------------------------------------------------
    # Base query
    # --------------------------------------------------

    # Используем Left Join (User -> MeterReading), чтобы видеть всех пользователей,
    # даже если у них еще нет записи показаний.
    stmt = (
        select(
            User.id,
            User.username,
            User.dormitory,

            MeterReading.initial_debt,
            MeterReading.initial_overpayment,
            MeterReading.total_cost
        )
        .outerjoin(
            MeterReading,
            (User.id == MeterReading.user_id) &
            (MeterReading.period_id == period_id)
        )
    )

    # --------------------------------------------------
    # Search filter
    # --------------------------------------------------

    if search:
        search_value = f"%{search.lower()}%"
        stmt = stmt.where(
            func.lower(User.username).like(search_value)
        )

    # --------------------------------------------------
    # Optimized Count Query
    # --------------------------------------------------

    # Вместо subquery() делаем прямой подсчет по ID, что быстрее
    count_stmt = select(func.count(User.id))

    # Применяем тот же фильтр поиска для подсчета
    if search:
        count_stmt = count_stmt.where(func.lower(User.username).like(search_value))

    total_res = await db.execute(count_stmt)
    total_items = total_res.scalar_one()

    # --------------------------------------------------
    # Pagination + order
    # --------------------------------------------------

    stmt = (
        stmt
        .order_by(User.dormitory, User.username)
        .limit(limit)
        .offset(offset)
    )

    # --------------------------------------------------
    # Execute
    # --------------------------------------------------

    result = await db.execute(stmt)
    rows = result.all()

    # --------------------------------------------------
    # Build response
    # --------------------------------------------------

    items: list[dict] = []

    for row in rows:
        items.append(
            {
                "id": row.id,
                "username": row.username,
                "dormitory": row.dormitory,

                "initial_debt": row.initial_debt or 0,
                "initial_overpayment": row.initial_overpayment or 0,
                "current_total_cost": row.total_cost or 0
            }
        )

    return {
        "total": total_items,
        "page": page,
        "size": limit,
        "items": items
    }