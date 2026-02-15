from typing import List
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import func
from decimal import Decimal

from app.database import get_db
from app.models import User, MeterReading, Tariff, BillingPeriod, Adjustment
from app.schemas import ReadingSchema, ReadingStateResponse
from app.dependencies import get_current_user
from app.services.calculations import calculate_utilities
from app.services.anomaly_detector import check_reading_for_anomalies
from app.services.pdf_generator import generate_receipt_pdf

router = APIRouter(tags=["Client Readings"])


@router.get("/api/readings/state", response_model=ReadingStateResponse)
async def get_reading_state(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Получение текущего состояния (последние показания, текущий черновик)"""
    # 1. Получаем текущий активный период
    period_res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = period_res.scalars().first()

    # 2. Получаем последнее утвержденное показание (для отображения "Предыдущие")
    prev_res = await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc()).limit(1)
    )
    prev = prev_res.scalars().first()

    # 3. Получаем текущий черновик (ТОЛЬКО в активном периоде)
    draft = None
    if active_period:
        draft_res = await db.execute(
            select(MeterReading)
            .where(
                MeterReading.user_id == current_user.id,
                MeterReading.is_approved == False,
                MeterReading.period_id == active_period.id
            )
        )
        draft = draft_res.scalars().first()

    zero_vol = Decimal("0.000")

    # Формируем ответ
    return {
        "period_name": active_period.name if active_period else "Прием показаний закрыт",

        "prev_hot": prev.hot_water if prev else zero_vol,
        "prev_cold": prev.cold_water if prev else zero_vol,
        "prev_elect": prev.electricity if prev else zero_vol,

        "current_hot": draft.hot_water if draft else None,
        "current_cold": draft.cold_water if draft else None,
        "current_elect": draft.electricity if draft else None,

        # total_cost здесь уже будет включать долг, если он был рассчитан при сохранении
        "total_cost": draft.total_cost if draft else None,

        "is_draft": True if draft else False,
        "is_period_open": True if active_period else False,

        # Детализация текущих начислений
        "cost_hot_water": draft.cost_hot_water if draft else None,
        "cost_cold_water": draft.cost_cold_water if draft else None,
        "cost_electricity": draft.cost_electricity if draft else None,
        "cost_sewage": draft.cost_sewage if draft else None,
        "cost_maintenance": draft.cost_maintenance if draft else None,
        "cost_social_rent": draft.cost_social_rent if draft else None,
        "cost_waste": draft.cost_waste if draft else None,
        "cost_fixed_part": draft.cost_fixed_part if draft else None,
    }


@router.post("/api/calculate")
async def save_reading(
        data: ReadingSchema,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Расчет и сохранение показаний (Черновик).
    """

    # 0. Проверяем, открыт ли период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()

    if not active_period:
        raise HTTPException(status_code=400, detail="Расчетный период закрыт. Передача показаний невозможна.")

    # 1. Загружаем АКТИВНЫЙ тариф
    t_res = await db.execute(select(Tariff).where(Tariff.is_active == True))
    t = t_res.scalars().first()
    if not t:
        raise HTTPException(status_code=500, detail="Активный тариф не найден")

    # 2. Получаем прошлые показания для валидации
    prev_res = await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc()).limit(1)
    )
    prev = prev_res.scalars().first()

    zero_vol = Decimal("0.000")
    p_hot = prev.hot_water if prev else zero_vol
    p_cold = prev.cold_water if prev else zero_vol
    p_elect = prev.electricity if prev else zero_vol

    # 3. Валидация (Нельзя вводить меньше предыдущего)
    if data.hot_water < p_hot:
        raise HTTPException(400, f"Г.В меньше предыдущей ({p_hot})")
    if data.cold_water < p_cold:
        raise HTTPException(400, f"Х.В меньше предыдущей ({p_cold})")
    if data.electricity < p_elect:
        raise HTTPException(400, f"Свет меньше предыдущего ({p_elect})")

    # 4. Расчет объемов (Дельта)
    d_hot = data.hot_water - p_hot
    d_cold = data.cold_water - p_cold
    d_elect_total = data.electricity - p_elect

    residents = Decimal(current_user.residents_count)
    total_residents_val = current_user.total_room_residents if current_user.total_room_residents > 0 else 1
    total_residents = Decimal(total_residents_val)

    user_share_kwh = (residents / total_residents) * d_elect_total
    vol_sewage = d_hot + d_cold

    # 5. Вызов сервиса расчетов (получаем стоимость текущего потребления)
    costs = calculate_utilities(
        user=current_user,
        tariff=t,
        volume_hot=d_hot,
        volume_cold=d_cold,
        volume_sewage=vol_sewage,
        volume_electricity_share=user_share_kwh
    )

    # <--- БЛОК ПРОВЕРКИ АНОМАЛИЙ --->
    history_res = await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc())
        .limit(4)
    )
    history = history_res.scalars().all()

    temp_reading = MeterReading(
        hot_water=data.hot_water,
        cold_water=data.cold_water,
        electricity=data.electricity
    )
    anomaly_flags = check_reading_for_anomalies(temp_reading, history, None)
    # <--- КОНЕЦ БЛОКА --->

    # 6. Получаем текущий черновик (если есть) с БЛОКИРОВКОЙ
    draft_res = await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == current_user.id,
            MeterReading.is_approved == False,
            MeterReading.period_id == active_period.id
        ).with_for_update()
    )
    draft = draft_res.scalars().first()

    # === РАСЧЕТ ИТОГОВОЙ СУММЫ С УЧЕТОМ ДОЛГОВ И КОРРЕКТИРОВОК ===
    current_debt = draft.initial_debt if draft and draft.initial_debt else Decimal("0.00")
    current_overpay = draft.initial_overpayment if draft and draft.initial_overpayment else Decimal("0.00")

    # Считаем корректировки (Adjustments), добавленные администратором
    adj_res = await db.execute(
        select(func.sum(Adjustment.amount))
        .where(Adjustment.user_id == current_user.id, Adjustment.period_id == active_period.id)
    )
    total_adjustments = adj_res.scalar() or Decimal("0.00")

    # Формула: Начисления за месяц + Долг - Переплата + Корректировки
    total_bill = costs["total_cost"] + current_debt - current_overpay + total_adjustments

    # 7. Сохранение / Обновление в БД
    if draft:
        draft.hot_water = data.hot_water
        draft.cold_water = data.cold_water
        draft.electricity = data.electricity

        # Обновляем поля стоимости услуг
        for k, v in costs.items():
            if hasattr(draft, k):
                setattr(draft, k, v)

        # Обновляем ИТОГОВУЮ сумму
        draft.total_cost = total_bill
        draft.anomaly_flags = anomaly_flags
    else:
        new_reading = MeterReading(
            user_id=current_user.id,
            period_id=active_period.id,
            hot_water=data.hot_water,
            cold_water=data.cold_water,
            electricity=data.electricity,

            # При создании новой записи клиентом долги по нулям,
            # если финансист еще не создал запись импортом
            initial_debt=Decimal("0.00"),
            initial_overpayment=Decimal("0.00"),

            # Сохраняем стоимости услуг
            **costs,

            is_approved=False,
            anomaly_flags=anomaly_flags
        )
        # Явно перезаписываем total_cost, чтобы включить adjustments и долги
        new_reading.total_cost = total_bill

        db.add(new_reading)

    # !!! ЯВНЫЙ КОММИТ !!!
    await db.commit()

    return {"status": "success", "total_cost": total_bill}


@router.get("/api/readings/history")
async def get_client_history(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Получение истории утвержденных начислений"""
    stmt = (
        select(MeterReading)
        .options(selectinload(MeterReading.period))
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc())
    )
    result = await db.execute(stmt)
    readings = result.scalars().all()

    history = []
    for r in readings:
        history.append({
            "id": r.id,
            "period": r.period.name if r.period else "Неизвестно",
            "hot": r.hot_water,
            "cold": r.cold_water,
            "electric": r.electricity,
            "total": r.total_cost,
            "date": r.created_at
        })
    return history


@router.get("/api/client/receipts/{reading_id}")
async def download_client_receipt(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """Генерация и скачивание квитанции для жильца"""

    # 1. Ищем запись и проверяем, принадлежит ли она пользователю
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

    if not reading:
        raise HTTPException(404, "Квитанция не найдена")

    # ГЛАВНАЯ ПРОВЕРКА БЕЗОПАСНОСТИ
    if reading.user_id != current_user.id:
        raise HTTPException(403, "Это не ваша квитанция")

    if not reading.is_approved:
        raise HTTPException(400, "Квитанция еще не сформирована (показания не утверждены)")

    # 2. Получаем АКТИВНЫЙ тариф
    tariff_res = await db.execute(select(Tariff).where(Tariff.is_active == True))
    tariff = tariff_res.scalars().first()
    if not tariff:
        raise HTTPException(500, "Активный тариф не найден")

    # 3. Получаем предыдущее показание (для расчета расхода в квитанции)
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

    # 4. Получаем корректировки для отображения в квитанции
    adj_stmt = select(Adjustment).where(
        Adjustment.user_id == reading.user_id,
        Adjustment.period_id == reading.period_id
    )
    adj_res = await db.execute(adj_stmt)
    adjustments = adj_res.scalars().all()

    try:
        # Генерируем PDF
        pdf_path = generate_receipt_pdf(
            user=reading.user,
            reading=reading,
            period=reading.period,
            tariff=tariff,
            prev_reading=prev,
            adjustments=adjustments
        )

        filename = f"receipt_{reading.period.name}.pdf"

        return FileResponse(
            path=pdf_path,
            filename=filename,
            media_type="application/pdf"
        )
    except Exception as e:
        print(f"Error generating PDF for client: {e}")
        raise HTTPException(500, "Ошибка формирования файла")