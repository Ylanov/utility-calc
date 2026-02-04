from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime

from app.database import get_db
from app.models import User, MeterReading, Tariff, BillingPeriod
from app.schemas import ReadingSchema, ReadingStateResponse
from app.dependencies import get_current_user
from app.services.calculations import calculate_utilities
from app.services.anomaly_detector import check_reading_for_anomalies

router = APIRouter(tags=["Client Readings"])


@router.get("/api/readings/state", response_model=ReadingStateResponse)
async def get_reading_state(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # 1. Получаем текущий активный период
    period_res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = period_res.scalars().first()

    # 2. Получаем последнее утвержденное показание (независимо от периода, просто последнее)
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

    return {
        "period_name": active_period.name if active_period else "Прием показаний закрыт",

        "prev_hot": prev.hot_water if prev else 0.0,
        "prev_cold": prev.cold_water if prev else 0.0,
        "prev_elect": prev.electricity if prev else 0.0,

        "current_hot": draft.hot_water if draft else None,
        "current_cold": draft.cold_water if draft else None,
        "current_elect": draft.electricity if draft else None,

        "total_cost": draft.total_cost if draft else None,
        "is_draft": True if draft else False,

        # --- ВАЖНОЕ ИСПРАВЛЕНИЕ: Передаем статус периода ---
        "is_period_open": True if active_period else False,
        # ---------------------------------------------------

        # Детализация
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
async def save_reading(data: ReadingSchema, current_user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    # 0. Проверяем, открыт ли период
    period_res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = period_res.scalars().first()

    if not active_period:
        raise HTTPException(status_code=400, detail="Расчетный период закрыт. Передача показаний невозможна.")

    # 1. Загружаем тарифы
    t_res = await db.execute(select(Tariff).where(Tariff.id == 1))
    t = t_res.scalars().first()

    # 2. Получаем прошлые показания для валидации
    prev_res = await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc()).limit(1)
    )
    prev = prev_res.scalars().first()

    p_hot = prev.hot_water if prev else 0.0
    p_cold = prev.cold_water if prev else 0.0
    p_elect = prev.electricity if prev else 0.0

    # 3. Валидация
    if data.hot_water < p_hot: raise HTTPException(400, f"Г.В меньше предыдущей ({p_hot})")
    if data.cold_water < p_cold: raise HTTPException(400, f"Х.В меньше предыдущей ({p_cold})")
    if data.electricity < p_elect: raise HTTPException(400, f"Свет меньше предыдущего ({p_elect})")

    # 4. Расчет объемов (Дельта)
    d_hot = data.hot_water - p_hot
    d_cold = data.cold_water - p_cold
    d_elect_total = data.electricity - p_elect

    # Расчет доли электричества
    total_residents = current_user.total_room_residents if current_user.total_room_residents > 0 else 1
    user_share_kwh = (current_user.residents_count / total_residents) * d_elect_total

    # Расчет объема водоотведения (сумма воды)
    vol_sewage = d_hot + d_cold

    # 5. Вызов сервиса расчетов
    costs = calculate_utilities(
        user=current_user,
        tariff=t,
        volume_hot=d_hot,
        volume_cold=d_cold,
        volume_sewage=vol_sewage,
        volume_electricity_share=user_share_kwh
    )

    # <--- БЛОК ПРОВЕРКИ АНОМАЛИЙ --->
    # Получаем историю для анализа (последние 4 утвержденных)
    history_res = await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc())
        .limit(4)
    )
    history = history_res.scalars().all()

    avg_peer_consumption = None
    # Здесь можно добавить логику получения средних показателей по общежитию

    # Создаем временный объект MeterReading для детектора
    temp_reading = MeterReading(
        hot_water=data.hot_water,
        cold_water=data.cold_water,
        electricity=data.electricity
    )
    anomaly_flags = check_reading_for_anomalies(temp_reading, history, avg_peer_consumption)
    # <--- КОНЕЦ БЛОКА АНОМАЛИЙ --->

    # 6. Сохранение в БД
    # Ищем черновик ТОЛЬКО В ТЕКУЩЕМ ПЕРИОДЕ
    draft_res = await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == current_user.id,
            MeterReading.is_approved == False,
            MeterReading.period_id == active_period.id
        )
    )
    draft = draft_res.scalars().first()

    if draft:
        draft.hot_water = data.hot_water
        draft.cold_water = data.cold_water
        draft.electricity = data.electricity

        # Обновляем все поля стоимости
        draft.total_cost = costs["total_cost"]
        draft.cost_hot_water = costs["cost_hot_water"]
        draft.cost_cold_water = costs["cost_cold_water"]
        draft.cost_sewage = costs["cost_sewage"]
        draft.cost_electricity = costs["cost_electricity"]
        draft.cost_maintenance = costs["cost_maintenance"]
        draft.cost_social_rent = costs["cost_social_rent"]
        draft.cost_waste = costs["cost_waste"]
        draft.cost_fixed_part = costs["cost_fixed_part"]

        # Обновляем флаги аномалий и дату
        draft.anomaly_flags = anomaly_flags
        draft.created_at = datetime.utcnow()
    else:
        new_reading = MeterReading(
            user_id=current_user.id,
            period_id=active_period.id,
            hot_water=data.hot_water,
            cold_water=data.cold_water,
            electricity=data.electricity,

            total_cost=costs["total_cost"],
            cost_hot_water=costs["cost_hot_water"],
            cost_cold_water=costs["cost_cold_water"],
            cost_sewage=costs["cost_sewage"],
            cost_electricity=costs["cost_electricity"],
            cost_maintenance=costs["cost_maintenance"],
            cost_social_rent=costs["cost_social_rent"],
            cost_waste=costs["cost_waste"],
            cost_fixed_part=costs["cost_fixed_part"],

            is_approved=False,
            # Добавляем флаги аномалий при создании
            anomaly_flags=anomaly_flags
        )
        db.add(new_reading)

    await db.commit()
    return {"status": "success", "total_cost": round(costs["total_cost"], 2)}