# Фоновая генерация квитанций: запуск задачи на один PDF, статус celery-задачи,
# массовый ZIP. Вербатим-перенос из admin_reports.py (строки 483-524), 1:1.

import asyncio
from typing import Optional

from fastapi import Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from celery.result import AsyncResult

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.utility.models import User, BillingPeriod
from app.modules.utility.services.s3_client import s3_service
from app.modules.utility.tasks import generate_receipt_task, start_bulk_receipt_generation

from ._shared import router


@router.post("/api/admin/receipts/{reading_id}/generate")
async def start_receipt_generation(reading_id: int, current_user: User = Depends(get_current_user)):
    if current_user.role not in ("accountant", "admin"): raise HTTPException(status_code=403)
    return {"task_id": generate_receipt_task.delay(reading_id).id, "status": "processing"}


@router.get("/api/admin/tasks/{task_id}")
async def get_task_status(task_id: str, current_user: User = Depends(get_current_user)):
    # Раньше любой авторизованный пользователь мог запросить статус
    # админской задачи и получить presigned-URL на PDF/Excel. Теперь
    # только admin (см. упрощение ролей — раньше было accountant/admin).
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Только для администратора")
    task_result = AsyncResult(task_id)
    if task_result.state == 'PENDING':
        return {"state": "PENDING", "status": "Pending..."}
    elif task_result.state != 'FAILURE':
        result = task_result.result
        if isinstance(result, dict) and result.get("status") in ["done", "ok"]:
            if s3_key := result.get("s3_key"):
                url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)
                return {"state": task_result.state, "status": "done", "download_url": url}
        return {"state": task_result.state, "result": result}
    else:
        return {"state": "FAILURE", "error": str(task_result.info)}


@router.post("/api/admin/reports/bulk-zip", summary="Сгенерировать ZIP архива квитанций")
async def create_bulk_zip(
        period_id: Optional[int] = Query(None, description="ID периода"),
        current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"): raise HTTPException(status_code=403)

    target_period_id = period_id
    if not target_period_id:
        period = (await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))).scalars().first()
        if not period: raise HTTPException(404, "Нет периодов")
        target_period_id = period.id

    task = start_bulk_receipt_generation.delay(target_period_id)
    return {"task_id": task.id, "status": "processing", "period_id": target_period_id}
