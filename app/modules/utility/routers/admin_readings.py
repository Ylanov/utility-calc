# app/modules/utility/routers/admin_readings.py

from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.utility.models import User
from app.modules.utility.schemas import ApproveRequest, AdminManualReadingSchema, OneTimeChargeSchema
from app.core.dependencies import RoleChecker

from app.modules.utility.services import admin_readings_list
from app.modules.utility.services import admin_readings_approve
from app.modules.utility.services import admin_readings_manual

router = APIRouter(tags=["Admin Readings"])

allow_readings_view = RoleChecker(["accountant", "admin", "financier"])
allow_readings_manage = RoleChecker(["accountant", "admin"])


@router.get("/api/admin/readings")
async def get_admin_readings(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=1000),
        cursor_id: Optional[int] = Query(None, description="Keyset pagination cursor"),
        direction: str = Query("next", pattern="^(next|prev)$"),
        search: Optional[str] = Query(None),
        anomalies_only: bool = Query(False),
        sort_by: str = Query("created_at"),
        sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
        # Волна 1 — расширенные фильтры реестра
        period_id: Optional[int] = Query(None, description="ID периода (default — активный)"),
        risk_level: Optional[str] = Query(None, pattern="^(clean|suspicious|critical)$"),
        flag_code: Optional[str] = Query(None, description="SPIKE_HOT / ZERO_BILL / ..."),
        source: Optional[str] = Query(None, pattern="^(user|gsheets|auto|one_time|meter_replace)$"),
        current_user: User = Depends(allow_readings_view),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_list.get_paginated_readings(
        db, page, limit, cursor_id, direction, search, anomalies_only, sort_by, sort_dir,
        period_id=period_id, risk_level=risk_level, flag_code=flag_code, source=source,
    )


@router.get("/api/admin/readings/stats")
async def get_admin_readings_stats(
        period_id: Optional[int] = Query(None),
        current_user: User = Depends(allow_readings_view),
        db: AsyncSession = Depends(get_db)
):
    """KPI для шапки реестра показаний (волна 1)."""
    return await admin_readings_list.get_readings_stats(db, period_id)


@router.get("/api/admin/readings/{reading_id}/decision-context")
async def get_reading_decision_context(
        reading_id: int,
        current_user: User = Depends(allow_readings_view),
        db: AsyncSession = Depends(get_db)
):
    """Расширенный контекст для раскрывающейся панели реестра (волна 2):
    история 4-х предыдущих утверждений, соседи, флаги + рекомендация."""
    return await admin_readings_list.get_decision_context(db, reading_id)


@router.post("/api/admin/approve-bulk")
async def bulk_approve_readings(
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_approve.bulk_approve_drafts(db, current_user)


@router.post("/api/admin/approve/{reading_id}")
async def approve_reading(
        reading_id: int,
        correction_data: ApproveRequest,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_approve.approve_single(db, reading_id, correction_data, current_user)


@router.delete("/api/admin/readings/{reading_id}")
async def delete_reading_record(
        reading_id: int,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_manual.delete_reading(db, reading_id, actor=current_user)


@router.post("/api/admin/readings/manual-receipt/{user_id}")
async def create_manual_receipt_endpoint(
        user_id: int,
        period_id: int | None = None,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db),
):
    """Создать квитанцию для жильца без подачи показаний (только долги/
    переплаты + фикс-часть тарифа). Использовать в финансовой отчётности
    когда у жильца есть debt от импорта 1С, но показания ещё не подал.

    Если total_209+total_205 < 0 → у жильца переплата (вернуть деньги или
    зачесть в следующем периоде). UI должен показать это как «остаток».
    """
    return await admin_readings_manual.create_manual_receipt(db, user_id, period_id)


@router.post("/api/admin/readings/manual-receipt-bulk")
async def bulk_create_manual_receipts_endpoint(
        period_id: int | None = None,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db),
):
    """Массово создать квитанции для всех жильцов которые НЕ подали
    показания в целевом периоде. Использует ту же логику что
    /manual-receipt/{user_id} — только сальдо, без начислений.

    Use case: в конце периода многие жильцы не подают показания. Админ
    одной кнопкой формирует им квитанции с актуальным сальдо (долги/
    переплаты из импорта 1С), не трогая тех у кого квитанция уже есть.
    """
    return await admin_readings_manual.bulk_create_manual_receipts(db, period_id)


@router.get("/api/admin/readings/manual-state/{user_id}")
async def get_manual_reading_state(
        user_id: int,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_list.get_manual_state(db, user_id)


@router.post("/api/admin/readings/manual")
async def save_manual_reading(
        data: AdminManualReadingSchema,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_manual.save_manual_entry(db, data)


@router.post("/api/admin/readings/one-time")
async def create_one_time_charge(
        data: OneTimeChargeSchema,
        current_user: User = Depends(allow_readings_manage),
        db: AsyncSession = Depends(get_db)
):
    return await admin_readings_manual.create_one_time_charge(db, data)
