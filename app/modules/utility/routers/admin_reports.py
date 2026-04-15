# app/modules/utility/routers/admin_reports.py

import io
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
from openpyxl import Workbook
from decimal import Decimal
from urllib.parse import quote

from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment, Room
from app.core.dependencies import get_current_user
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.s3_client import s3_service
from app.modules.utility.tasks import generate_receipt_task, start_bulk_receipt_generation

router = APIRouter(tags=["Admin Reports"])
ZERO = Decimal("0.00")


@router.get("/api/admin/receipts/{reading_id}")
async def get_receipt_pdf(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    stmt = (
        select(MeterReading)
        .options(selectinload(MeterReading.user).selectinload(User.room), selectinload(MeterReading.period))
        .where(MeterReading.id == reading_id)
    )
    reading = (await db.execute(stmt)).scalars().first()

    if not reading or not reading.user or not reading.period or not reading.user.room:
        raise HTTPException(404, "Данные не найдены или жилец не привязан к помещению")

    user, room = reading.user, reading.user.room

    tariff = (
        await db.execute(select(Tariff).where(Tariff.id == (getattr(user, 'tariff_id', None) or 1)))).scalars().first()
    if not tariff:
        tariff = (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()
        if not tariff: raise HTTPException(404, "Активный тариф не найден")

    prev = (await db.execute(
        select(MeterReading)
        .where(MeterReading.room_id == room.id, MeterReading.is_approved.is_(True),
               MeterReading.created_at < reading.created_at)
        .order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    adjustments = (await db.execute(
        select(Adjustment).where(Adjustment.user_id == user.id, Adjustment.period_id == reading.period_id)
    )).scalars().all()

    try:
        pdf_path = await asyncio.to_thread(
            generate_receipt_pdf,
            user=user, room=room, reading=reading, period=reading.period,
            tariff=tariff, prev_reading=prev, adjustments=adjustments, output_dir="/tmp"
        )
        s3_key = f"receipts/{reading.period.id}/admin_view_{user.id}_{uuid.uuid4().hex[:8]}.pdf"

        if await asyncio.to_thread(s3_service.upload_file, pdf_path, s3_key):
            await asyncio.to_thread(os.remove, pdf_path)
            download_url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)
            return {"status": "success", "url": download_url}
        else:
            # S3 недоступен — перемещаем PDF в статику и отдаём прямую ссылку
            import shutil
            static_dir = "/app/static/generated_files"
            await asyncio.to_thread(os.makedirs, static_dir, exist_ok=True)
            filename = os.path.basename(pdf_path)
            static_path = os.path.join(static_dir, filename)
            await asyncio.to_thread(shutil.move, pdf_path, static_path)
            return {"status": "success", "url": f"/generated_files/{filename}"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Ошибка генерации PDF: {e}")


@router.get("/api/admin/export_report", summary="Скачать отчет Excel (XLSX)")
async def export_report(
        period_id: Optional[int] = Query(None, description="ID периода"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(status_code=403)

    target_period_id = period_id
    if not target_period_id:
        active_period = (
            await db.execute(select(BillingPeriod).where(BillingPeriod.is_active.is_(True)))).scalars().first()
        if active_period:
            target_period_id = active_period.id
        else:
            last_closed = (
                await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))).scalars().first()
            if not last_closed:
                raise HTTPException(404, "Нет периодов для отчета")
            target_period_id = last_closed.id

    period = await db.get(BillingPeriod, target_period_id)
    if not period: raise HTTPException(404, "Выбранный период не найден")

    # ИСПРАВЛЕНИЕ: Используем потоковое чтение (yield_per) из БД, чтобы не грузить все в RAM
    statement = (
        select(User, MeterReading, Room)
        .join(MeterReading, User.id == MeterReading.user_id)
        .join(Room, User.room_id == Room.id)
        .where(
            MeterReading.period_id == target_period_id,
            MeterReading.is_approved.is_(True)
        )
        .order_by(Room.dormitory_name, Room.room_number, User.username)
    ).execution_options(yield_per=1000)

    # ИСПРАВЛЕНИЕ: Используем write_only=True для потоковой записи Excel
    # (openpyxl не хранит документ в памяти, а пишет напрямую в байтовый буфер)
    workbook = Workbook(write_only=True)
    worksheet = workbook.create_sheet("Сводная ведомость")

    headers = ["Общежитие/Комната", "ФИО (Логин)", "Площадь", "Жильцов", "ГВС (руб)", "ХВС (руб)", "Водоотв. (руб)",
               "Электроэнергия (руб)", "Содержание (руб)", "Наем (руб)", "ТКО (руб)", "Отопление + ОДН (руб)",
               "Счет 209 (Комм.)", "Счет 205 (Найм)", "ИТОГО (руб)"]
    worksheet.append(headers)

    total_sum, total_209, total_205 = ZERO, ZERO, ZERO

    # Потоковое получение результатов
    result = await db.stream(statement)

    async for row in result:
        user, reading, room = row
        total_cost = Decimal(reading.total_cost or 0)
        t_209 = Decimal(reading.total_209 or 0)
        t_205 = Decimal(reading.total_205 or 0)

        total_sum += total_cost
        total_209 += t_209
        total_205 += t_205

        worksheet.append([
            f"{room.dormitory_name} / {room.room_number}",
            user.username.split("_deleted_")[0] if user.is_deleted else user.username,
            room.apartment_area,
            f"{user.residents_count}/{room.total_room_residents}",
            reading.cost_hot_water, reading.cost_cold_water, reading.cost_sewage, reading.cost_electricity,
            reading.cost_maintenance, reading.cost_social_rent, reading.cost_waste, reading.cost_fixed_part,
            t_209, t_205, total_cost
        ])

    worksheet.append([""] * 12 + ["ИТОГО:", total_209, total_205, total_sum])
    filename = f"Report_{period.name.replace(' ', '_')}.xlsx"
    encoded_filename = quote(filename)

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)

    return StreamingResponse(
        output,
        headers={'Content-Disposition': f"attachment; filename*=utf-8''{encoded_filename}"},
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


@router.post("/api/admin/receipts/{reading_id}/generate")
async def start_receipt_generation(reading_id: int, current_user: User = Depends(get_current_user)):
    if current_user.role not in ("accountant", "admin"): raise HTTPException(status_code=403)
    return {"task_id": generate_receipt_task.delay(reading_id).id, "status": "processing"}


@router.get("/api/admin/tasks/{task_id}")
async def get_task_status(task_id: str, current_user: User = Depends(get_current_user)):
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


@router.get("/api/admin/summary")
async def get_accountant_summary(
        period_id: Optional[int] = Query(None),
        current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"): raise HTTPException(status_code=403, detail="Доступ запрещен")

    # ИСПРАВЛЕНИЕ: Добавляем yield_per, чтобы не грузить весь массив в RAM разом.
    stmt = (
        select(User, MeterReading, Room)
        .join(MeterReading, User.id == MeterReading.user_id)
        .join(Room, User.room_id == Room.id)
        .where(MeterReading.is_approved.is_(True))
    ).execution_options(yield_per=1000)

    if period_id:
        stmt = stmt.where(MeterReading.period_id == period_id)
    else:
        last_period = (
            await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))).scalars().first()
        if not last_period: return {}
        stmt = stmt.where(MeterReading.period_id == last_period.id)

    stmt = stmt.order_by(Room.dormitory_name, Room.room_number, User.username)

    summary = {}

    # Потоковое получение
    result = await db.stream(stmt)
    async for row in result:
        user, reading, room = row
        dorm = room.dormitory_name or "Без общежития"
        if dorm not in summary: summary[dorm] = []
        summary[dorm].append({
            "reading_id": reading.id, "user_id": user.id, "username": user.username, "area": room.apartment_area,
            "residents": user.residents_count, "hot": reading.cost_hot_water or 0, "cold": reading.cost_cold_water or 0,
            "sewage": reading.cost_sewage or 0, "electric": reading.cost_electricity or 0,
            "maintenance": reading.cost_maintenance or 0, "rent": reading.cost_social_rent or 0,
            "waste": reading.cost_waste or 0, "fixed": reading.cost_fixed_part or 0,
            "total_cost": reading.total_cost or 0, "total_209": reading.total_209 or 0,
            "total_205": reading.total_205 or 0,
            "date": reading.created_at.strftime("%Y-%m-%d %H:%M")
        })

    return summary