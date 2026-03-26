# app/modules/utility/routers/admin_readings.py
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.utility.models import User
from app.modules.utility.schemas import ApproveRequest, AdminManualReadingSchema, OneTimeChargeSchema
from app.core.dependencies import get_current_user

# Импортируем наши новые разделенные сервисы
from app.modules.utility.services import admin_readings_list
from app.modules.utility.services import admin_readings_approve
from app.modules.utility.services import admin_readings_manual

router = APIRouter(tags=["Admin Readings"])

def check_role(user: User, allowed_roles: list):
    if user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")


@router.get("/api/admin/readings")
async def get_admin_readings(
        page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=1000),
        after_id: Optional[int] = Query(None, description="Keyset пагинация"),
        search: Optional[str] = Query(None), anomalies_only: bool = Query(False),
        sort_by: str = Query("created_at"), sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
        current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    check_role(current_user, ["accountant", "admin", "financier"])
    return await admin_readings_list.get_paginated_readings(db, page, limit, after_id, search, anomalies_only, sort_by, sort_dir)


@router.post("/api/admin/approve-bulk")
async def bulk_approve_readings(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    check_role(current_user, ["accountant", "admin"])
    return await admin_readings_approve.bulk_approve_drafts(db)


@router.post("/api/admin/approve/{reading_id}")
async def approve_reading(
        reading_id: int, correction_data: ApproveRequest,
        current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    check_role(current_user, ["accountant", "admin"])
    return await admin_readings_approve.approve_single(db, reading_id, correction_data)


@router.delete("/api/admin/readings/{reading_id}")
async def delete_reading_record(
        reading_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    check_role(current_user,["accountant", "admin"])
    return await admin_readings_manual.delete_reading(db, reading_id)


@router.get("/api/admin/readings/manual-state/{user_id}")
async def get_manual_reading_state(
        user_id: int, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    check_role(current_user, ["accountant", "admin"])
    return await admin_readings_list.get_manual_state(db, user_id)


@router.post("/api/admin/readings/manual")
async def save_manual_reading(
        data: AdminManualReadingSchema, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    check_role(current_user, ["accountant", "admin"])
    return await admin_readings_manual.save_manual_entry(db, data)


@router.post("/api/admin/readings/one-time")
async def create_one_time_charge(
        data: OneTimeChargeSchema, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    check_role(current_user, ["accountant", "admin"])
    return await admin_readings_manual.create_one_time_charge(db, data)