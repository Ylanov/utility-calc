from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from celery.result import AsyncResult

from app.database import get_db
from app.models import User, MeterReading, Tariff, BillingPeriod
from app.dependencies import get_current_user
from app.services.pdf_generator import generate_receipt_pdf
from app.services.excel_service import generate_billing_report_xlsx
# ИМПОРТИРУЕМ НОВУЮ ЗАДАЧУ-СТАРТЕР
from app.tasks import generate_receipt_task, start_bulk_receipt_generation

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
        .options(
            selectinload(MeterReading.user),
            selectinload(MeterReading.period)
        )
        .where(MeterReading.id == reading_id)
    )

    res = await db.execute(stmt)
    reading = res.scalars().first()

    if not reading or not reading.user or not reading.period:
        raise HTTPException(404, "Данные не найдены")

    tariff_res = await db.execute(select(Tariff).where(Tariff.id == 1))
    tariff = tariff_res.scalars().first()

    if not tariff:
        raise HTTPException(404, "Тариф не найден")

    # Получаем предыдущее показание
    prev_stmt = (
        select(MeterReading)
        .where(
            MeterReading.user_id == reading.user_id,
            MeterReading.is_approved == True,
            MeterReading.created_at < reading.created_at
        )
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )

    prev_res = await db.execute(prev_stmt)
    prev = prev_res.scalars().first()

    try:
        # generate_receipt_pdf внутренне использует Decimal при расчетах объемов
        pdf_path = generate_receipt_pdf(
            user=reading.user,
            reading=reading,
            period=reading.period,
            tariff=tariff,
            prev_reading=prev
        )

        filename = f"receipt_{reading.user.username}_{reading.period.name}.pdf"

        return FileResponse(
            path=pdf_path,
            filename=filename,
            media_type="application/pdf"
        )

    except Exception as e:
        print("PDF error:", e)
        raise HTTPException(500, "Ошибка генерации PDF")


@router.get("/api/admin/export_report", summary="Скачать отчет Excel (XLSX)")
async def export_report(
        period_id: Optional[int] = Query(None, description="ID периода"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403)

    target_period_id = period_id

    # Если ID не передан, ищем текущий активный или последний
    if not target_period_id:
        res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
        period = res.scalars().first()

        if not period:
            res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
            period = res.scalars().first()

        if not period:
            raise HTTPException(404, "Нет периодов для отчета")

        target_period_id = period.id

    output, filename = await generate_billing_report_xlsx(db, target_period_id)

    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"'
    }

    return StreamingResponse(
        output,
        headers=headers,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )


# Эндпоинт запуска генерации в фоне (для одиночного файла)
@router.post("/api/admin/receipts/{reading_id}/generate")
async def start_receipt_generation(
        reading_id: int,
        current_user: User = Depends(get_current_user)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # Запускаем задачу в Celery
    task = generate_receipt_task.delay(reading_id)

    return {"task_id": task.id, "status": "processing"}


# Эндпоинт проверки статуса задачи
@router.get("/api/admin/tasks/{task_id}")
async def get_task_status(task_id: str, current_user: User = Depends(get_current_user)):
    task_result = AsyncResult(task_id)

    if task_result.state == 'PENDING':
        return {"state": "PENDING", "status": "Pending..."}
    elif task_result.state != 'FAILURE':
        result = task_result.result
        # Проверяем формат ответа (для ZIP и для PDF он схожий)
        if isinstance(result, dict) and result.get("status") == "done":
            # Для ZIP может не быть ключа filename в корне, но путь должен быть корректным
            # Если это ZIP задача, download_url формируется так же
            filename = result.get('filename', 'archive.zip')
            return {
                "state": task_result.state,
                "status": "done",
                "download_url": f"/static/generated_files/{filename}"
            }
        return {"state": task_result.state, "result": result}
    else:
        return {"state": "FAILURE", "error": str(task_result.info)}


# --- НОВЫЙ ЭНДПОИНТ: МАССОВАЯ ГЕНЕРАЦИЯ (ZIP) ---
@router.post("/api/admin/reports/bulk-zip", summary="Сгенерировать ZIP архива квитанций")
async def create_bulk_zip(
        period_id: Optional[int] = Query(None, description="ID периода"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    target_period_id = period_id

    # Если ID не передан, ищем последний период
    if not target_period_id:
        res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
        period = res.scalars().first()
        if not period:
            raise HTTPException(404, "Нет периодов")
        target_period_id = period.id

    # Запускаем БЫСТРУЮ задачу-стартер
    # Она вернет ID цепочки, который нужно отслеживать
    task = start_bulk_receipt_generation.delay(target_period_id)

    # Мы должны подождать совсем немного, чтобы получить ID реальной цепочки задач
    # start_bulk_receipt_generation работает очень быстро (доли секунды), так как только читает ID из базы
    try:
        # Ждем результат (словарь с task_id)
        result_data = task.get(timeout=10)
        final_task_id = result_data['task_id']
    except Exception as e:
        print(f"Error launching bulk tasks: {e}")
        raise HTTPException(500, "Ошибка запуска массовой генерации")

    return {"task_id": final_task_id, "status": "processing", "period_id": target_period_id}