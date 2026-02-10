from typing import Dict, List, Optional
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import aliased
from sqlalchemy import desc

from app.database import get_db
# ИЗМЕНЕНИЕ: Добавлен импорт модели Adjustment
from app.models import User, MeterReading, Tariff, BillingPeriod, Adjustment
from app.schemas import ApproveRequest
from app.dependencies import get_current_user
from app.services.calculations import calculate_utilities, D

router = APIRouter(tags=["Admin Readings"])

# ===================================================================
# КАРТА ДЛЯ ДЕТАЛИЗАЦИИ АНОМАЛИЙ
# ===================================================================
ANOMALY_MAP: Dict[str, Dict[str, str]] = {
    "NEGATIVE_HOT": {"message": "Ошибка: Текущие показания ГВС меньше предыдущих!", "severity": "high"},
    "NEGATIVE_COLD": {"message": "Ошибка: Текущие показания ХВС меньше предыдущих!", "severity": "high"},
    "NEGATIVE_ELECT": {"message": "Ошибка: Текущие показания электричества меньше предыдущих!", "severity": "high"},
    "HIGH_HOT": {"message": "Очень высокий расход горячей воды по сравнению с историей.", "severity": "medium"},
    "HIGH_COLD": {"message": "Очень высокий расход холодной воды по сравнению с историей.", "severity": "medium"},
    "HIGH_ELECT": {"message": "Очень высокий расход электричества по сравнению с историей.", "severity": "medium"},
    "HIGH_VS_PEERS_HOT": {"message": "Расход ГВС значительно выше среднего по общежитию.", "severity": "medium"},
    "HIGH_VS_PEERS_COLD": {"message": "Расход ХВС значительно выше среднего по общежитию.", "severity": "medium"},
    "HIGH_VS_PEERS_ELECT": {"message": "Расход электричества значительно выше среднего по общежитию.",
                            "severity": "medium"},
    "ZERO_HOT": {"message": "Нулевой расход горячей воды (возможно, комната пустует).", "severity": "low"},
    "ZERO_COLD": {"message": "Нулевой расход холодной воды (возможно, комната пустует).", "severity": "low"},
    "ZERO_ELECT": {"message": "Нулевой расход электричества (возможно, ком-та пустует).", "severity": "low"},
    "FROZEN_HOT": {"message": "Показания счетчика ГВС не менялись 3+ месяца.", "severity": "low"},
    "FROZEN_COLD": {"message": "Показания счетчика ХВС не менялись 3+ месяца.", "severity": "low"},
    "FROZEN_ELECT": {"message": "Показания счетчика света не менялись 3+ месяца.", "severity": "low"},
    "UNKNOWN": {"message": "Обнаружена неопознанная аномалия.", "severity": "low"}
}


@router.get("/api/admin/readings")
async def get_admin_readings(
        page: int = Query(1, ge=1, description="Номер страницы"),
        limit: int = Query(50, ge=1, le=100, description="Записей на странице"),
        anomalies_only: bool = Query(False, description="Только аномальные"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    period_res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = period_res.scalars().first()

    if not active_period:
        return []

    offset = (page - 1) * limit

    # Подзапрос для получения предыдущих показаний
    prev_subq = (
        select(MeterReading)
        .where(MeterReading.is_approved == True)
        .distinct(MeterReading.user_id)
        .order_by(MeterReading.user_id, MeterReading.created_at.desc())
        .subquery()
    )
    prev_alias = aliased(MeterReading, prev_subq)

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

    if anomalies_only:
        stmt = stmt.where(MeterReading.anomaly_flags != None)

    results = await db.execute(stmt)

    data = []
    # Константа для нуля
    zero = Decimal("0.000")

    for current, user, prev in results.all():
        anomaly_details = []
        if current.anomaly_flags:
            flags = current.anomaly_flags.split(',')
            for flag_code in flags:
                details = ANOMALY_MAP.get(flag_code, ANOMALY_MAP["UNKNOWN"])
                anomaly_details.append({
                    "code": flag_code,
                    "message": details["message"],
                    "severity": details["severity"]
                })

        data.append({
            "id": current.id,
            "user_id": user.id,
            "username": user.username,
            "dormitory": user.dormitory,

            # Используем D() или явное преобразование, чтобы вернуть Decimal
            "prev_hot": prev.hot_water if prev else zero,
            "cur_hot": current.hot_water,
            "prev_cold": prev.cold_water if prev else zero,
            "cur_cold": current.cold_water,
            "prev_elect": prev.electricity if prev else zero,
            "cur_elect": current.electricity,

            "total_cost": current.total_cost,
            "residents_count": user.residents_count,
            "total_room_residents": user.total_room_residents,
            "created_at": current.created_at,
            "anomaly_flags": current.anomaly_flags,
            "anomaly_details": anomaly_details,
        })

    return data


@router.post("/api/admin/approve-bulk")
async def bulk_approve_readings(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Массовое утверждение всех черновиков в активном периоде.
    Утверждаются только те, где нет критических ошибок (например, отрицательный расход).
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # 1. Получаем активный период
    period_res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = period_res.scalars().first()

    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода")

    # 2. Получаем все неутвержденные показания для этого периода
    stmt = (
        select(MeterReading)
        .where(
            MeterReading.is_approved == False,
            MeterReading.period_id == active_period.id
        )
    )
    result = await db.execute(stmt)
    drafts = result.scalars().all()

    approved_count = 0
    zero = Decimal("0.000")

    for draft in drafts:
        # Получаем предыдущее утвержденное показание для проверки
        prev_res = await db.execute(
            select(MeterReading)
            .where(MeterReading.user_id == draft.user_id, MeterReading.is_approved == True)
            .order_by(MeterReading.created_at.desc())
            .limit(1)
        )
        prev = prev_res.scalars().first()

        # Приводим к Decimal
        p_hot = D(prev.hot_water) if prev else zero
        p_cold = D(prev.cold_water) if prev else zero
        p_elect = D(prev.electricity) if prev else zero

        cur_hot = D(draft.hot_water)
        cur_cold = D(draft.cold_water)
        cur_elect = D(draft.electricity)

        # Простая проверка: если текущее меньше предыдущего, пропускаем (требует ручного вмешательства)
        if cur_hot < p_hot or cur_cold < p_cold or cur_elect < p_elect:
            continue

        # Утверждаем
        draft.is_approved = True
        approved_count += 1

    await db.commit()
    return {"status": "success", "approved_count": approved_count}


@router.post("/api/admin/approve/{reading_id}")
async def approve_reading(
        reading_id: int,
        correction_data: ApproveRequest,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    reading = await db.get(MeterReading, reading_id)
    if not reading:
        raise HTTPException(status_code=404, detail="Показания не найдены")

    user = await db.get(User, reading.user_id)
    t_res = await db.execute(select(Tariff).where(Tariff.id == 1))
    t = t_res.scalars().first()

    # Получаем предыдущие показания
    prev_res = await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )
    prev = prev_res.scalars().first()

    # Инициализация Decimal
    zero = Decimal("0.000")
    p_hot = D(prev.hot_water) if prev else zero
    p_cold = D(prev.cold_water) if prev else zero
    p_elect = D(prev.electricity) if prev else zero

    # Текущие показания тоже приводим к Decimal
    cur_hot = D(reading.hot_water)
    cur_cold = D(reading.cold_water)
    cur_elect = D(reading.electricity)

    # Расчет "сырой" дельты (без учета коррекции)
    d_hot_raw = cur_hot - p_hot
    d_cold_raw = cur_cold - p_cold
    d_elect_total = cur_elect - p_elect

    # Применение коррекции (вычитаем коррекцию из объема)
    # correction_data уже Decimal из Pydantic
    d_hot_final = d_hot_raw - correction_data.hot_correction
    d_cold_final = d_cold_raw - correction_data.cold_correction

    # Расчет доли электричества
    residents = Decimal(user.residents_count)
    total_residents_val = user.total_room_residents if user.total_room_residents > 0 else 1
    total_residents = Decimal(total_residents_val)

    user_share_kwh = (residents / total_residents) * d_elect_total

    # Коррекция электричества применяется к доле пользователя
    d_elect_final = user_share_kwh - correction_data.electricity_correction

    # Водоотведение
    vol_sewage_base = d_hot_final + d_cold_final
    vol_sewage_final = vol_sewage_base - correction_data.sewage_correction

    # Пересчет стоимости коммунальных услуг
    costs = calculate_utilities(
        user=user,
        tariff=t,
        volume_hot=d_hot_final,
        volume_cold=d_cold_final,
        volume_sewage=vol_sewage_final,
        volume_electricity_share=d_elect_final
    )

    # --- ИЗМЕНЕНИЕ: Учет ручных корректировок (Adjustments) ---
    # Ищем все корректировки для этого пользователя в этом периоде
    adj_res = await db.execute(
        select(Adjustment).where(
            Adjustment.user_id == user.id,
            Adjustment.period_id == reading.period_id
        )
    )
    adjustments = adj_res.scalars().all()

    # Суммируем все корректировки
    total_adjustment_amount = sum(adj.amount for adj in adjustments)

    # Добавляем корректировки к итоговой сумме
    final_total_cost = costs["total_cost"] + total_adjustment_amount

    # Сохранение коррекций объемов
    reading.hot_correction = correction_data.hot_correction
    reading.cold_correction = correction_data.cold_correction
    reading.electricity_correction = correction_data.electricity_correction
    reading.sewage_correction = correction_data.sewage_correction

    # Сохранение пересчитанных стоимостей
    reading.total_cost = final_total_cost  # Используем сумму с учетом Adjustments
    reading.cost_hot_water = costs["cost_hot_water"]
    reading.cost_cold_water = costs["cost_cold_water"]
    reading.cost_sewage = costs["cost_sewage"]
    reading.cost_electricity = costs["cost_electricity"]
    reading.cost_maintenance = costs["cost_maintenance"]
    reading.cost_social_rent = costs["cost_social_rent"]
    reading.cost_waste = costs["cost_waste"]
    reading.cost_fixed_part = costs["cost_fixed_part"]

    reading.is_approved = True

    await db.commit()

    return {"status": "approved", "new_total": final_total_cost}


@router.get("/api/admin/summary")
async def get_accountant_summary(
        period_id: Optional[int] = Query(None, description="ID периода для фильтрации"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # Базовый запрос: соединяем Users и MeterReading
    stmt = (
        select(User, MeterReading)
        .join(MeterReading, User.id == MeterReading.user_id)
        .where(MeterReading.is_approved == True)
    )

    # Фильтрация по периоду
    if period_id:
        stmt = stmt.where(MeterReading.period_id == period_id)
    else:
        # Если период не указан, берем самый свежий
        last_period_res = await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))
        last_period = last_period_res.scalars().first()
        if last_period:
            stmt = stmt.where(MeterReading.period_id == last_period.id)

    stmt = stmt.order_by(MeterReading.created_at.desc())

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