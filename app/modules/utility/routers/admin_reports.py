import os
import uuid
import asyncio
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from celery.result import AsyncResult

from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment
from app.core.dependencies import get_current_user
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.excel_service import generate_billing_report_xlsx
from app.modules.utility.services.s3_client import s3_service
from app.modules.utility.tasks import generate_receipt_task, start_bulk_receipt_generation

router = APIRouter(tags=["Admin Reports"])


@router.get("/api/admin/receipts/{reading_id}")
async def get_receipt_pdf(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    stmt = (
        select(MeterReading)
        .options(selectinload(MeterReading.user), selectinload(MeterReading.period))
        .where(MeterReading.id == reading_id)
    )
    res = await db.execute(stmt)
    reading = res.scalars().first()

    if not reading or not reading.user or not reading.period:
        raise HTTPException(404, "Данные не найдены")

    user_tariff_id = getattr(reading.user, 'tariff_id', None) or 1
    tariff_res = await db.execute(select(Tariff).where(Tariff.id == user_tariff_id))
    tariff = tariff_res.scalars().first()

    if not tariff:
        tariff_res_def = await db.execute(select(Tariff).where(Tariff.is_active))
        tariff = tariff_res_def.scalars().first()
        if not tariff:
            raise HTTPException(404, "Активный тариф не найден")

    prev_stmt = (
        select(MeterReading)
        .where(
            MeterReading.user_id == reading.user_id,
            MeterReading.is_approved,
            MeterReading.created_at < reading.created_at
        )
        .order_by(MeterReading.created_at.desc()).limit(1)
    )
    prev_res = await db.execute(prev_stmt)
    prev = prev_res.scalars().first()

    adj_stmt = select(Adjustment).where(
        Adjustment.user_id == reading.user_id,
        Adjustment.period_id == reading.period_id
    )
    adj_res = await db.execute(adj_stmt)
    adjustments = adj_res.scalars().all()

    try:
        temp_dir = "/tmp"

        # Выносим рендеринг PDF в отдельный поток
        pdf_path = await asyncio.to_thread(
            generate_receipt_pdf,
            user=reading.user,
            reading=reading,
            period=reading.period,
            tariff=tariff,
            prev_reading=prev,
            adjustments=adjustments,
            output_dir=temp_dir
        )

        s3_key = f"receipts/{reading.period.id}/admin_view_{reading.user.id}_{uuid.uuid4().hex[:8]}.pdf"

        # Синхронный вызов Boto3 вынесен в отдельный поток
        upload_success = await asyncio.to_thread(s3_service.upload_file, pdf_path, s3_key)

        if upload_success:
            await asyncio.to_thread(os.remove, pdf_path)
            download_url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)

            # Возвращаем JSON со ссылкой (Решение проблемы с CORS)
            return {"status": "success", "url": download_url}
        else:
            raise HTTPException(500, "Ошибка загрузки файла в хранилище")

    except Exception as e:
        print("PDF error:", e)
        raise HTTPException(500, f"Ошибка генерации PDF: {e}")


@router.get("/api/admin/export_report", summary="Скачать отчет Excel (XLSX)")
async def export_report(
        period_id: Optional[int] = Query(None, description="ID периода"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403)

    target_period_id = period_id
    if not target_period_id:
        res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))
        period = res.scalars().first()
        if not period:
            res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
            period = res.scalars().first()
        if not period:
            raise HTTPException(404, "Нет периодов для отчета")
        target_period_id = period.id

    output, filename = await generate_billing_report_xlsx(db, target_period_id)
    headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
    return StreamingResponse(
        output, headers=headers,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@router.post("/api/admin/receipts/{reading_id}/generate")
async def start_receipt_generation(
        reading_id: int,
        current_user: User = Depends(get_current_user)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403)
    task = generate_receipt_task.delay(reading_id)
    return {"task_id": task.id, "status": "processing"}


@router.get("/api/admin/tasks/{task_id}")
async def get_task_status(task_id: str, current_user: User = Depends(get_current_user)):
    task_result = AsyncResult(task_id)

    if task_result.state == 'PENDING':
        return {"state": "PENDING", "status": "Pending..."}

    elif task_result.state != 'FAILURE':
        result = task_result.result

        if isinstance(result, dict) and result.get("status") in ["done", "ok"]:
            s3_key = result.get("s3_key")

            if s3_key:
                # Синхронный запрос к S3 вынесен в поток
                download_url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)
                return {
                    "state": task_result.state,
                    "status": "done",
                    "download_url": download_url
                }

            filename = result.get('filename', 'document.pdf')
            return {
                "state": task_result.state,
                "status": "done",
                "download_url": f"/static/generated_files/{filename}"
            }

        return {"state": task_result.state, "result": result}

    else:
        return {"state": "FAILURE", "error": str(task_result.info)}


@router.post("/api/admin/reports/bulk-zip", summary="Сгенерировать ZIP архива квитанций")
async def create_bulk_zip(
        period_id: Optional[int] = Query(None, description="ID периода"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403)

    target_period_id = period_id
    if not target_period_id:
        res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
        period = res.scalars().first()
        if not period:
            raise HTTPException(404, "Нет периодов")
        target_period_id = period.id

    task = start_bulk_receipt_generation.delay(target_period_id)
    try:
        # get() блокирует поток. Оборачиваем в asyncio.to_thread
        result_data = await asyncio.to_thread(task.get, timeout=10)
        final_task_id = result_data['task_id']
    except Exception as e:
        print(f"Error launching bulk tasks: {e}")
        raise HTTPException(500, "Ошибка запуска массовой генерации")

    return {"task_id": final_task_id, "status": "processing", "period_id": target_period_id}


@router.get("/api/admin/summary")
async def get_accountant_summary(
        period_id: Optional[int] = Query(None, description="ID периода для фильтрации"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    stmt = (
        select(User, MeterReading)
        .join(MeterReading, User.id == MeterReading.user_id)
        .where(MeterReading.is_approved)
    )

    if period_id:
        stmt = stmt.where(MeterReading.period_id == period_id)
    else:
        last_period_res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
        last_period = last_period_res.scalars().first()
        if last_period:
            stmt = stmt.where(MeterReading.period_id == last_period.id)
        else:
            return {}

    stmt = stmt.order_by(User.dormitory, User.username)
    result = await db.execute(stmt)
    summary = {}

    for user, reading in result:
        dorm = user.dormitory or "Без общежития"
        if dorm not in summary:
            summary[dorm] = []

        summary[dorm].append({
            "reading_id": reading.id,
            "user_id": user.id,
            "username": user.username,
            "area": user.apartment_area,
            "residents": user.residents_count,
            "hot": reading.cost_hot_water or 0,
            "cold": reading.cost_cold_water or 0,
            "sewage": reading.cost_sewage or 0,
            "electric": reading.cost_electricity or 0,
            "maintenance": reading.cost_maintenance or 0,
            "rent": reading.cost_social_rent or 0,
            "waste": reading.cost_waste or 0,
            "fixed": reading.cost_fixed_part or 0,
            "total_cost": reading.total_cost or 0,
            "total_209": reading.total_209 or 0,
            "total_205": reading.total_205 or 0,
            "date": reading.created_at.strftime("%Y-%m-%d %H:%M")
        })

    return summary