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

from app.core.database import get_db
# ИЗМЕНЕНИЕ: Добавляем импорт Room
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
        # ИЗМЕНЕНИЕ: Подгружаем комнату жильца одним запросом
        .options(selectinload(MeterReading.user).selectinload(User.room), selectinload(MeterReading.period))
        .where(MeterReading.id == reading_id)
    )
    res = await db.execute(stmt)
    reading = res.scalars().first()

    if not reading or not reading.user or not reading.period or not reading.user.room:
        raise HTTPException(404, "Данные не найдены или жилец не привязан к помещению")

    user = reading.user
    room = user.room

    user_tariff_id = getattr(user, 'tariff_id', None) or 1
    tariff_res = await db.execute(select(Tariff).where(Tariff.id == user_tariff_id))
    tariff = tariff_res.scalars().first()

    if not tariff:
        tariff_res_def = await db.execute(select(Tariff).where(Tariff.is_active))
        tariff = tariff_res_def.scalars().first()
        if not tariff:
            raise HTTPException(404, "Активный тариф не найден")

    # ИЗМЕНЕНИЕ: Ищем историю по room_id
    prev_stmt = (
        select(MeterReading)
        .where(
            MeterReading.room_id == room.id,
            MeterReading.is_approved,
            MeterReading.created_at < reading.created_at
        )
        .order_by(MeterReading.created_at.desc()).limit(1)
    )
    prev_res = await db.execute(prev_stmt)
    prev = prev_res.scalars().first()

    adj_stmt = select(Adjustment).where(
        Adjustment.user_id == user.id,
        Adjustment.period_id == reading.period_id
    )
    adj_res = await db.execute(adj_stmt)
    adjustments = adj_res.scalars().all()

    try:
        temp_dir = "/tmp"

        # ИЗМЕНЕНИЕ: Передаем room в функцию генерации
        pdf_path = await asyncio.to_thread(
            generate_receipt_pdf,
            user=user,
            room=room,  # <--- ПЕРЕДАЕМ КОМНАТУ
            reading=reading,
            period=reading.period,
            tariff=tariff,
            prev_reading=prev,
            adjustments=adjustments,
            output_dir=temp_dir
        )

        s3_key = f"receipts/{reading.period.id}/admin_view_{user.id}_{uuid.uuid4().hex[:8]}.pdf"
        upload_success = await asyncio.to_thread(s3_service.upload_file, pdf_path, s3_key)

        if upload_success:
            await asyncio.to_thread(os.remove, pdf_path)
            download_url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)
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
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(status_code=403)

    target_period_id = period_id
    if not target_period_id:
        res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
        period = res.scalars().first()
        if not period:
            raise HTTPException(404, "Нет периодов для отчета")
        target_period_id = period.id

    # --- ГЛОБАЛЬНОЕ ИЗМЕНЕНИЕ ЗАПРОСА ---
    statement = (
        select(User, MeterReading, Room)
        .join(MeterReading, User.id == MeterReading.user_id)
        .join(Room, User.room_id == Room.id)  # <-- СОЕДИНЯЕМ КОМНАТУ
        .where(
            MeterReading.period_id == target_period_id,
            MeterReading.is_approved.is_(True)
        )
        .order_by(Room.dormitory_name, Room.room_number, User.username)
    )

    result = await db.execute(statement)

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Сводная ведомость"

    headers = [
        "Общежитие/Комната", "ФИО (Логин)", "Площадь", "Жильцов",
        "ГВС (руб)", "ХВС (руб)", "Водоотв. (руб)", "Электроэнергия (руб)",
        "Содержание (руб)", "Наем (руб)", "ТКО (руб)", "Отопление + ОДН (руб)",
        "Счет 209 (Комм.)", "Счет 205 (Найм)", "ИТОГО (руб)"
    ]
    worksheet.append(headers)

    total_sum, total_209_sum, total_205_sum = ZERO, ZERO, ZERO

    for user, reading, room in result:  # <-- Теперь у нас есть и room
        total_cost, t_209, t_205 = Decimal(reading.total_cost or 0), Decimal(reading.total_209 or 0), Decimal(
            reading.total_205 or 0)
        total_sum += total_cost
        total_209_sum += t_209
        total_205_sum += t_205

        username_display = user.username.split("_deleted_")[0] + " (Выселен)" if user.is_deleted else user.username

        worksheet.append([
            f"{room.dormitory_name} / {room.room_number}",  # Данные из комнаты
            username_display,
            room.apartment_area,  # Данные из комнаты
            f"{user.residents_count}/{room.total_room_residents}",  # Данные из user и room
            reading.cost_hot_water, reading.cost_cold_water, reading.cost_sewage,
            reading.cost_electricity, reading.cost_maintenance, reading.cost_social_rent,
            reading.cost_waste, reading.cost_fixed_part,
            t_209, t_205, total_cost
        ])

    worksheet.append([""] * 11 + ["ИТОГО:", total_209_sum, total_205_sum, total_sum])

    period = await db.get(BillingPeriod, target_period_id)
    filename = f"Report_{period.name.replace(' ', '_')}.xlsx"

    output = io.BytesIO()
    workbook.save(output)
    output.seek(0)

    headers = {'Content-Disposition': f'attachment; filename="{filename}"'}
    return StreamingResponse(
        output, headers=headers,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


# Остальные функции (работа с задачами Celery) остаются без изменений
@router.post("/api/admin/receipts/{reading_id}/generate")
async def start_receipt_generation(
        reading_id: int,
        current_user: User = Depends(get_current_user)
):
    if current_user.role not in ("accountant", "admin"):
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
                download_url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)
                return {"state": task_result.state, "status": "done", "download_url": download_url}
        return {"state": task_result.state, "result": result}
    else:
        return {"state": "FAILURE", "error": str(task_result.info)}


@router.post("/api/admin/reports/bulk-zip", summary="Сгенерировать ZIP архива квитанций")
async def create_bulk_zip(
        period_id: Optional[int] = Query(None, description="ID периода"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(status_code=403)

    target_period_id = period_id
    if not target_period_id:
        res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
        period = res.scalars().first()
        if not period: raise HTTPException(404, "Нет периодов")
        target_period_id = period.id

    task_result = start_bulk_receipt_generation.delay(target_period_id)
    return {"task_id": task_result.id, "status": "processing", "period_id": target_period_id}

@router.get("/api/admin/summary")
async def get_accountant_summary(
        period_id: Optional[int] = Query(None, description="ID периода для фильтрации"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # --- ИЗМЕНЕНИЕ: Запрос теперь соединяет три таблицы ---
    stmt = (
        select(User, MeterReading, Room)
        .join(MeterReading, User.id == MeterReading.user_id)
        .join(Room, User.room_id == Room.id) # <-- НОВОЕ СОЕДИНЕНИЕ
        .where(MeterReading.is_approved)
    )

    if period_id:
        stmt = stmt.where(MeterReading.period_id == period_id)
    else:
        # Логика поиска последнего периода остается
        last_period_res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
        last_period = last_period_res.scalars().first()
        if last_period:
            stmt = stmt.where(MeterReading.period_id == last_period.id)
        else:
            return {} # Возвращаем пустой объект, если периодов нет

    # --- ИЗМЕНЕНИЕ: Сортируем по данным из Room ---
    stmt = stmt.order_by(Room.dormitory_name, Room.room_number, User.username)
    result = await db.execute(stmt)
    summary = {}

    # --- ИЗМЕНЕНИЕ: В цикле теперь есть объект room ---
    for user, reading, room in result:
        dorm = room.dormitory_name or "Без общежития"
        if dorm not in summary:
            summary[dorm] = []

        summary[dorm].append({
            "reading_id": reading.id,
            "user_id": user.id,
            "username": user.username,
            "area": room.apartment_area,           # <-- Данные из комнаты
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
