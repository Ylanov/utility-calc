from typing import Optional, List
import os
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import aliased
from sqlalchemy import desc

from app.database import get_db
from app.models import User, MeterReading, Tariff, BillingPeriod
from app.schemas import ApproveRequest, PeriodCreate, PeriodResponse
from app.dependencies import get_current_user
from app.services.calculations import calculate_utilities
from app.services.pdf_generator import generate_receipt_pdf
from fastapi.responses import FileResponse
from sqlalchemy.orm import selectinload
from app.services.billing import close_period_and_generate_missing
from fastapi.responses import StreamingResponse
from app.services.excel_service import generate_billing_report_xlsx

router = APIRouter(tags=["Admin Readings"])


# -------------------------------------------------
# 1. ПОЛУЧЕНИЕ ПОКАЗАНИЙ НА ПРОВЕРКУ (ЧЕРНОВИКИ)
# -------------------------------------------------
@router.get("/api/admin/readings")
async def get_admin_readings(
        page: int = Query(1, ge=1, description="Номер страницы"),
        limit: int = Query(50, ge=1, le=100, description="Записей на странице"),
        anomalies_only: bool = Query(False, description="Только аномальные"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Возвращает список неутвержденных показаний (черновиков)
    ТОЛЬКО для текущего активного периода.
    Включает оптимизацию N+1 для получения предыдущих показаний.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # 1. Находим активный период
    period_res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = period_res.scalars().first()

    # Если периода нет, то и показаний быть не может
    if not active_period:
        return []

    offset = (page - 1) * limit

    # 2. ОПТИМИЗАЦИЯ SQL (Решение проблемы N+1)
    # Нам нужно найти ПОСЛЕДНЕЕ УТВЕРЖДЕННОЕ показание для каждого юзера,
    # чтобы показать бухгалтеру разницу (было -> стало).
    # Это показание могло быть сделано в ПРОШЛОМ периоде, поэтому фильтр по периоду тут не нужен.

    prev_subq = (
        select(MeterReading)
        .where(MeterReading.is_approved == True)
        .distinct(MeterReading.user_id)  # PostgreSQL specific: оставляет одну (последнюю) запись
        .order_by(MeterReading.user_id, MeterReading.created_at.desc())
        .subquery()
    )

    # Создаем алиас для join'а
    prev_alias = aliased(MeterReading, prev_subq)

    # 3. Основной запрос
    stmt = (
        select(MeterReading, User, prev_alias)
        .join(User, MeterReading.user_id == User.id)
        .outerjoin(prev_alias, MeterReading.user_id == prev_alias.user_id)
        .where(
            MeterReading.is_approved == False,
            MeterReading.period_id == active_period.id
        )
        .order_by(MeterReading.created_at.desc())
        .offset(offset)
        .limit(limit)
    )

    # ДОБАВЛЯЕМ ФИЛЬТР
    if anomalies_only:
        stmt = stmt.where(MeterReading.anomaly_flags != None)

    results = await db.execute(stmt)

    data = []
    for current, user, prev in results:
        data.append({
            "id": current.id,
            "user_id": user.id,
            "username": user.username,
            "dormitory": user.dormitory,

            # Предыдущие показания (или 0.0, если это первый ввод)
            "prev_hot": prev.hot_water if prev else 0.0,
            "cur_hot": current.hot_water,

            "prev_cold": prev.cold_water if prev else 0.0,
            "cur_cold": current.cold_water,

            "prev_elect": prev.electricity if prev else 0.0,
            "cur_elect": current.electricity,

            "total_cost": current.total_cost,
            "residents_count": user.residents_count,
            "total_room_residents": user.total_room_residents,
            "created_at": current.created_at,
            "anomaly_flags": current.anomaly_flags # <--- НОВОЕ ПОЛЕ

        })

    return data


# -------------------------------------------------
# 2. УТВЕРЖДЕНИЕ ПОКАЗАНИЙ (С КОРРЕКЦИЯМИ)
# -------------------------------------------------
@router.post("/api/admin/approve/{reading_id}")
async def approve_reading(
        reading_id: int,
        correction_data: ApproveRequest,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # 1. Получаем данные записи и пользователя
    reading = await db.get(MeterReading, reading_id)
    if not reading:
        raise HTTPException(status_code=404, detail="Показания не найдены")

    user = await db.get(User, reading.user_id)

    # 2. Получаем тарифы
    t_res = await db.execute(select(Tariff).where(Tariff.id == 1))
    t = t_res.scalars().first()

    # 3. Получаем предыдущие показания (для расчета расхода)
    prev_res = await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )
    prev = prev_res.scalars().first()

    p_hot = prev.hot_water if prev else 0.0
    p_cold = prev.cold_water if prev else 0.0
    p_elect = prev.electricity if prev else 0.0

    # 4. Считаем "грязный" расход (Дельта: Текущее - Предыдущее)
    d_hot_raw = reading.hot_water - p_hot
    d_cold_raw = reading.cold_water - p_cold
    d_elect_total = reading.electricity - p_elect

    # 5. Применяем КОРРЕКЦИИ (введенные бухгалтером)
    # Расход = (Текущее - Предыдущее) - Коррекция

    d_hot_final = d_hot_raw - correction_data.hot_correction
    d_cold_final = d_cold_raw - correction_data.cold_correction

    # Для света: Сначала считаем долю жильца, потом вычитаем коррекцию
    total_residents = user.total_room_residents if user.total_room_residents > 0 else 1
    user_share_kwh = (user.residents_count / total_residents) * d_elect_total
    d_elect_final = user_share_kwh - correction_data.electricity_correction

    # Для водоотведения: Сумма воды - Коррекция водоотведения
    vol_sewage_base = d_hot_final + d_cold_final
    vol_sewage_final = vol_sewage_base - correction_data.sewage_correction

    # 6. Вызываем сервис расчетов (передаем уже скорректированные объемы)
    costs = calculate_utilities(
        user=user,
        tariff=t,
        volume_hot=d_hot_final,
        volume_cold=d_cold_final,
        volume_sewage=vol_sewage_final,
        volume_electricity_share=d_elect_final
    )

    # 7. Сохраняем введенные коррекции в базу
    reading.hot_correction = correction_data.hot_correction
    reading.cold_correction = correction_data.cold_correction
    reading.electricity_correction = correction_data.electricity_correction
    reading.sewage_correction = correction_data.sewage_correction

    # 8. Сохраняем рассчитанные суммы (ВСЕ ПОЛЯ)
    reading.total_cost = costs["total_cost"]

    reading.cost_hot_water = costs["cost_hot_water"]
    reading.cost_cold_water = costs["cost_cold_water"]
    reading.cost_sewage = costs["cost_sewage"]
    reading.cost_electricity = costs["cost_electricity"]
    reading.cost_maintenance = costs["cost_maintenance"]

    reading.cost_social_rent = costs["cost_social_rent"]
    reading.cost_waste = costs["cost_waste"]
    reading.cost_fixed_part = costs["cost_fixed_part"]

    # 9. Утверждаем
    reading.is_approved = True

    await db.commit()

    return {"status": "approved", "new_total": costs["total_cost"]}


# -------------------------------------------------
# 3. ПОЛУЧЕНИЕ СВОДКИ (БУХГАЛТЕРИЯ)
# -------------------------------------------------
@router.get("/api/admin/summary")
async def get_accountant_summary(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # Берем ВСЕ утвержденные показания
    # (В будущем можно добавить фильтр по period_id)
    stmt = (
        select(User, MeterReading)
        .join(MeterReading, User.id == MeterReading.user_id)
        .where(MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc())
    )

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

            # Финансовая детализация
            "hot": reading.cost_hot_water,
            "cold": reading.cost_cold_water,
            "sewage": reading.cost_sewage,
            "electric": reading.cost_electricity,
            "maintenance": reading.cost_maintenance,
            "rent": reading.cost_social_rent,
            "waste": reading.cost_waste,
            "fixed": reading.cost_fixed_part,
            "total": reading.total_cost,

            "date": reading.created_at.strftime("%Y-%m-%d %H:%M")
        })

    return summary


# -------------------------------------------------
# 4. УДАЛЕНИЕ ЗАПИСИ (РУЧНОЕ УПРАВЛЕНИЕ)
# -------------------------------------------------
@router.delete("/api/admin/readings/{reading_id}")
async def delete_reading_record(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    reading = await db.get(MeterReading, reading_id)
    if not reading:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    await db.delete(reading)
    await db.commit()

    return {"status": "deleted"}


# -------------------------------------------------
# 5. УПРАВЛЕНИЕ ПЕРИОДАМИ (ЗАКРЫТИЕ МЕСЯЦА)
# -------------------------------------------------

@router.post("/api/admin/periods", summary="Закрыть текущий и открыть новый месяц")
async def create_period(
        data: PeriodCreate,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Закрывает текущий месяц.
    Всем, кто не подал показания, начисляет автоматически 'по среднему'.
    Открывает новый месяц.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    try:
        result = await close_period_and_generate_missing(
            db=db,
            new_period_name=data.name,
            admin_user_id=current_user.id
        )
        return result
    except Exception as e:
        print(f"Error closing period: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка закрытия периода: {str(e)}")


@router.get("/api/admin/periods/active", response_model=Optional[PeriodResponse], summary="Текущий активный месяц")
async def get_active_period(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    return res.scalars().first()


# -------------------------------------------------
# 6. ГЕНЕРАЦИЯ PDF КВИТАНЦИИ
# -------------------------------------------------
@router.get("/api/admin/receipts/{reading_id}")
async def get_receipt_pdf(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # Получаем показание + связи
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

    # Тариф
    tariff_res = await db.execute(select(Tariff).where(Tariff.id == 1))
    tariff = tariff_res.scalars().first()

    if not tariff:
        raise HTTPException(404, "Тариф не найден")

    # Предыдущие показания
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

    # Генерация PDF
    try:

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
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403)

    # Определяем активный или последний закрытый период
    # В идеале передавать period_id как параметр, но пока берем активный
    # Если активного нет (междумесячье), берем последний закрытый

    # 1. Активный?
    res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    period = res.scalars().first()

    if not period:
        # Берем последний
        res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
        period = res.scalars().first()

    if not period:
        raise HTTPException(404, "Нет периодов для отчета")

    output, filename = await generate_billing_report_xlsx(db, period.id)

    headers = {
        'Content-Disposition': f'attachment; filename="{filename}"'
    }

    return StreamingResponse(output, headers=headers,
                             media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')