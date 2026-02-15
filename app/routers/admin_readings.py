from typing import Dict, List, Optional
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, func

from app.database import get_db
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
        select(
            MeterReading.user_id,
            MeterReading.hot_water.label("prev_hot"),
            MeterReading.cold_water.label("prev_cold"),
            MeterReading.electricity.label("prev_elect")
        )
        .where(MeterReading.is_approved == True)
        .distinct(MeterReading.user_id)
        .order_by(MeterReading.user_id, MeterReading.created_at.desc())
        .subquery()
    )

    stmt = (
        select(MeterReading, User, prev_subq)
        .join(User, MeterReading.user_id == User.id)
        .outerjoin(prev_subq, MeterReading.user_id == prev_subq.c.user_id)
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
    zero = Decimal("0.000")

    for row in results.all():
        current = row[0]  # MeterReading
        user = row[1]  # User

        p_hot = getattr(row, "prev_hot", zero)
        p_cold = getattr(row, "prev_cold", zero)
        p_elect = getattr(row, "prev_elect", zero)

        prev_hot = p_hot if p_hot is not None else zero
        prev_cold = p_cold if p_cold is not None else zero
        prev_elect = p_elect if p_elect is not None else zero

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

            "prev_hot": prev_hot,
            "cur_hot": current.hot_water,
            "prev_cold": prev_cold,
            "cur_cold": current.cold_water,
            "prev_elect": prev_elect,
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
    ИСПРАВЛЕНО: Убран db.begin(), добавлен явный commit.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # --- ИЗМЕНЕНИЕ: Убрали async with db.begin(): ---
    # 1. Получаем активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()

    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода")

    # 2. Получаем активный тариф
    res_tariff = await db.execute(select(Tariff).where(Tariff.is_active == True))
    tariff = res_tariff.scalars().first()
    if not tariff:
        raise HTTPException(500, detail="Активный тариф не найден")

    # 3. Получаем все неутвержденные показания
    stmt_drafts = (
        select(MeterReading, User)
        .join(User, MeterReading.user_id == User.id)
        .where(
            MeterReading.is_approved == False,
            MeterReading.period_id == active_period.id
        )
        .with_for_update()
    )
    res_drafts = await db.execute(stmt_drafts)
    drafts_rows = res_drafts.all()

    if not drafts_rows:
        return {"status": "success", "approved_count": 0}

    user_ids = [row[0].user_id for row in drafts_rows]

    # 4. Загружаем последние утвержденные показания
    subq_max_date = (
        select(
            MeterReading.user_id,
            func.max(MeterReading.created_at).label("max_created")
        )
        .where(
            MeterReading.user_id.in_(user_ids),
            MeterReading.is_approved == True
        )
        .group_by(MeterReading.user_id)
        .subquery()
    )

    stmt_prev = (
        select(MeterReading)
        .join(
            subq_max_date,
            (MeterReading.user_id == subq_max_date.c.user_id) &
            (MeterReading.created_at == subq_max_date.c.max_created)
        )
    )

    res_prev = await db.execute(stmt_prev)
    prev_readings_map = {r.user_id: r for r in res_prev.scalars().all()}

    # 5. Загружаем корректировки (Adjustments)
    stmt_adj = select(Adjustment).where(
        Adjustment.period_id == active_period.id,
        Adjustment.user_id.in_(user_ids)
    )
    res_adj = await db.execute(stmt_adj)

    adj_map = {}
    zero = Decimal("0.000")
    for adj in res_adj.scalars().all():
        adj_map.setdefault(adj.user_id, zero)
        adj_map[adj.user_id] += adj.amount

    approved_count = 0

    # 6. Обработка и расчет
    for reading, user in drafts_rows:
        if reading.is_approved:
            continue

        prev = prev_readings_map.get(reading.user_id)

        p_hot = D(prev.hot_water) if prev else zero
        p_cold = D(prev.cold_water) if prev else zero
        p_elect = D(prev.electricity) if prev else zero

        cur_hot = D(reading.hot_water)
        cur_cold = D(reading.cold_water)
        cur_elect = D(reading.electricity)

        if cur_hot < p_hot or cur_cold < p_cold or cur_elect < p_elect:
            continue

        d_hot = cur_hot - p_hot
        d_cold = cur_cold - p_cold
        d_elect_total = cur_elect - p_elect

        residents = Decimal(user.residents_count)
        total_res = Decimal(user.total_room_residents if user.total_room_residents > 0 else 1)
        user_elect_share = (residents / total_res) * d_elect_total

        costs = calculate_utilities(
            user=user,
            tariff=tariff,
            volume_hot=d_hot,
            volume_cold=d_cold,
            volume_sewage=d_hot + d_cold,
            volume_electricity_share=user_elect_share
        )

        total_adj = adj_map.get(user.id, zero)
        final_total = costs["total_cost"] + total_adj

        reading.total_cost = final_total
        for k, v in costs.items():
            if hasattr(reading, k):
                setattr(reading, k, v)

        reading.is_approved = True
        approved_count += 1

    # !!! ЯВНЫЙ КОММИТ !!!
    await db.commit()

    return {"status": "success", "approved_count": approved_count}


@router.post("/api/admin/approve/{reading_id}")
async def approve_reading(
        reading_id: int,
        correction_data: ApproveRequest,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Утверждение одной записи с возможной коррекцией.
    ИСПРАВЛЕНО: Убран db.begin(), добавлен явный commit.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # --- ИЗМЕНЕНИЕ: Убрали async with db.begin(): ---
    # Блокируем строку для предотвращения Race Condition
    res_reading = await db.execute(
        select(MeterReading)
        .where(MeterReading.id == reading_id)
        .with_for_update()
    )
    reading = res_reading.scalars().first()

    if not reading:
        raise HTTPException(status_code=404, detail="Показания не найдены")

    if reading.is_approved:
        raise HTTPException(status_code=400, detail="Показания уже утверждены")

    user = await db.get(User, reading.user_id)

    res_t = await db.execute(select(Tariff).where(Tariff.is_active == True))
    t = res_t.scalars().first()
    if not t:
        raise HTTPException(status_code=500, detail="Активный тариф не найден")

    prev_res = await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )
    prev = prev_res.scalars().first()

    zero = Decimal("0.000")
    p_hot = D(prev.hot_water) if prev else zero
    p_cold = D(prev.cold_water) if prev else zero
    p_elect = D(prev.electricity) if prev else zero

    cur_hot = D(reading.hot_water)
    cur_cold = D(reading.cold_water)
    cur_elect = D(reading.electricity)

    d_hot_raw = cur_hot - p_hot
    d_cold_raw = cur_cold - p_cold
    d_elect_total = cur_elect - p_elect

    d_hot_final = d_hot_raw - correction_data.hot_correction
    d_cold_final = d_cold_raw - correction_data.cold_correction

    residents = Decimal(user.residents_count)
    total_residents_val = user.total_room_residents if user.total_room_residents > 0 else 1
    total_residents = Decimal(total_residents_val)

    user_share_kwh = (residents / total_residents) * d_elect_total

    d_elect_final = user_share_kwh - correction_data.electricity_correction

    vol_sewage_base = d_hot_final + d_cold_final
    vol_sewage_final = vol_sewage_base - correction_data.sewage_correction

    costs = calculate_utilities(
        user=user,
        tariff=t,
        volume_hot=d_hot_final,
        volume_cold=d_cold_final,
        volume_sewage=vol_sewage_final,
        volume_electricity_share=d_elect_final
    )

    adj_res = await db.execute(
        select(func.sum(Adjustment.amount))
        .where(
            Adjustment.user_id == user.id,
            Adjustment.period_id == reading.period_id
        )
    )
    total_adjustment_amount = adj_res.scalar() or zero

    final_total_cost = costs["total_cost"] + total_adjustment_amount

    reading.hot_correction = correction_data.hot_correction
    reading.cold_correction = correction_data.cold_correction
    reading.electricity_correction = correction_data.electricity_correction
    reading.sewage_correction = correction_data.sewage_correction

    reading.total_cost = final_total_cost
    reading.cost_hot_water = costs["cost_hot_water"]
    reading.cost_cold_water = costs["cost_cold_water"]
    reading.cost_sewage = costs["cost_sewage"]
    reading.cost_electricity = costs["cost_electricity"]
    reading.cost_maintenance = costs["cost_maintenance"]
    reading.cost_social_rent = costs["cost_social_rent"]
    reading.cost_waste = costs["cost_waste"]
    reading.cost_fixed_part = costs["cost_fixed_part"]

    reading.is_approved = True

    # !!! ЯВНЫЙ КОММИТ !!!
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

    stmt = (
        select(User, MeterReading)
        .join(MeterReading, User.id == MeterReading.user_id)
        .where(MeterReading.is_approved == True)
    )

    if period_id:
        stmt = stmt.where(MeterReading.period_id == period_id)
    else:
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