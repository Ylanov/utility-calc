from typing import Dict, List, Optional, Any
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, asc, func, or_, update, case

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


# ===================================================================
# ПОЛУЧЕНИЕ СПИСКА ПОКАЗАНИЙ (С ПАГИНАЦИЕЙ)
# ===================================================================
@router.get("/api/admin/readings")
async def get_admin_readings(
        page: int = Query(1, ge=1, description="Номер страницы"),
        limit: int = Query(50, ge=1, le=1000, description="Записей на странице"),
        search: Optional[str] = Query(None, description="Поиск по жильцу"),
        anomalies_only: bool = Query(False, description="Только аномальные"),
        sort_by: str = Query("created_at", description="Поле сортировки"),
        sort_dir: str = Query("desc", description="Направление сортировки (asc/desc)"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Получение списка неутвержденных показаний (черновиков) за текущий период.
    Оптимизировано для больших объемов данных (избегает тяжелых JOIN подзапросов).
    """
    allowed_roles = ["accountant", "admin", "financier"]
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # 1. Получаем активный период
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()

    if not active_period:
        return {"total": 0, "page": page, "size": limit, "items": []}

    # 2. Формируем запрос для списка текущих показаний
    query = (
        select(MeterReading, User)
        .join(User, MeterReading.user_id == User.id)
        .where(
            MeterReading.is_approved == False,
            MeterReading.period_id == active_period.id
        )
    )

    # Фильтры
    if anomalies_only:
        query = query.where(MeterReading.anomaly_flags != None)

    if search:
        search_fmt = f"%{search}%"
        # Для PostgreSQL и больших данных здесь желателен индекс pg_trgm
        query = query.where(
            or_(
                User.username.ilike(search_fmt),
                User.dormitory.ilike(search_fmt)
            )
        )

    # 3. Подсчет общего количества (Total) - быстрый count
    count_query = select(func.count()).select_from(query.subquery())
    total_res = await db.execute(count_query)
    total = total_res.scalar_one()

    # 4. Сортировка
    if sort_by == "username":
        sort_col = User.username
    elif sort_by == "dormitory":
        sort_col = User.dormitory
    elif sort_by == "total_cost":
        sort_col = MeterReading.total_cost
    else:
        sort_col = MeterReading.created_at

    if sort_dir == "asc":
        query = query.order_by(asc(sort_col))
    else:
        query = query.order_by(desc(sort_col))

    # 5. Пагинация и получение основной выборки
    offset = (page - 1) * limit
    query = query.offset(offset).limit(limit)

    results = await db.execute(query)
    rows = results.all()

    if not rows:
        return {"total": total, "page": page, "size": limit, "items": []}

    # 6. Эффективная подгрузка предыдущих показаний (Batch Fetching)
    # Вместо JOIN подзапроса, берем ID юзеров текущей страницы и делаем один запрос IN (...)
    user_ids = [row[1].id for row in rows]

    subq_max_prev = (
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
            subq_max_prev,
            (MeterReading.user_id == subq_max_prev.c.user_id) &
            (MeterReading.created_at == subq_max_prev.c.max_created)
        )
    )

    res_prev = await db.execute(stmt_prev)
    prev_map = {r.user_id: r for r in res_prev.scalars().all()}

    # 7. Формирование ответа
    items = []
    zero = Decimal("0.000")

    for current, user in rows:
        prev = prev_map.get(user.id)

        prev_hot = prev.hot_water if prev else zero
        prev_cold = prev.cold_water if prev else zero
        prev_elect = prev.electricity if prev else zero

        # Детализация аномалий
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

        items.append({
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

    return {
        "total": total,
        "page": page,
        "size": limit,
        "items": items
    }


# ===================================================================
# МАССОВОЕ УТВЕРЖДЕНИЕ (BULK UPDATE)
# ===================================================================
@router.post("/api/admin/approve-bulk")
async def bulk_approve_readings(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Массовое утверждение всех черновиков в активном периоде.
    Использует Bulk Update Mappings для максимальной производительности.
    """
    allowed_roles = ["accountant", "admin"]
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # 1. Активный период и тариф
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода")

    res_tariff = await db.execute(select(Tariff).where(Tariff.is_active == True))
    tariff = res_tariff.scalars().first()
    if not tariff:
        raise HTTPException(500, detail="Активный тариф не найден")

    # 2. Получаем все черновики
    stmt_drafts = (
        select(MeterReading, User)
        .join(User, MeterReading.user_id == User.id)
        .where(
            MeterReading.is_approved == False,
            MeterReading.period_id == active_period.id
        )
    )
    res_drafts = await db.execute(stmt_drafts)
    drafts_rows = res_drafts.all()

    if not drafts_rows:
        return {"status": "success", "approved_count": 0}

    user_ids = [row[0].user_id for row in drafts_rows]

    # 3. Получаем предыдущие показания (Batch Fetch)
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

    # 4. Получаем корректировки, сгруппированные по user_id и account_type
    stmt_adj = select(
        Adjustment.user_id,
        Adjustment.account_type,
        func.sum(Adjustment.amount).label("total")
    ).where(
        Adjustment.period_id == active_period.id,
        Adjustment.user_id.in_(user_ids)
    ).group_by(Adjustment.user_id, Adjustment.account_type)

    res_adj = await db.execute(stmt_adj)

    # Карта: user_id -> {'209': Decimal, '205': Decimal}
    adj_map = {}
    zero = Decimal("0.000")

    for uid, acc_type, amount in res_adj.all():
        if uid not in adj_map:
            adj_map[uid] = {'209': zero, '205': zero}
        adj_map[uid][str(acc_type)] = amount or zero

    # 5. Подготовка данных для Bulk Update
    update_mappings = []

    for reading, user in drafts_rows:
        prev = prev_readings_map.get(reading.user_id)

        p_hot = D(prev.hot_water) if prev else zero
        p_cold = D(prev.cold_water) if prev else zero
        p_elect = D(prev.electricity) if prev else zero

        cur_hot = D(reading.hot_water)
        cur_cold = D(reading.cold_water)
        cur_elect = D(reading.electricity)

        # Пропускаем ошибочные показания (меньше предыдущих)
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

        # Финансовые расчеты (с учетом разделения счетов)
        user_adjs = adj_map.get(user.id, {'209': zero, '205': zero})

        cost_rent_205 = costs['cost_social_rent']
        cost_utils_209 = costs['total_cost'] - cost_rent_205

        # Долги уже есть в reading (подтянуты при создании или импорте)
        d_209 = reading.debt_209 or zero
        o_209 = reading.overpayment_209 or zero
        d_205 = reading.debt_205 or zero
        o_205 = reading.overpayment_205 or zero

        total_209 = cost_utils_209 + d_209 - o_209 + user_adjs['209']
        total_205 = cost_rent_205 + d_205 - o_205 + user_adjs['205']
        grand_total = total_209 + total_205

        # Собираем словарь обновлений для этой строки
        update_data = {
            "id": reading.id,
            "is_approved": True,
            "total_cost": grand_total,
            "total_209": total_209,
            "total_205": total_205,
            **costs  # Распаковка полей стоимости (cost_hot_water и т.д.)
        }
        update_mappings.append(update_data)

    # 6. Выполняем массовое обновление одним запросом
    if update_mappings:
        await db.execute(update(MeterReading), update_mappings)
        await db.commit()

    return {"status": "success", "approved_count": len(update_mappings)}


# ===================================================================
# УТВЕРЖДЕНИЕ ОДНОЙ ЗАПИСИ
# ===================================================================
@router.post("/api/admin/approve/{reading_id}")
async def approve_reading(
        reading_id: int,
        correction_data: ApproveRequest,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    allowed_roles = ["accountant", "admin"]
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    res_reading = await db.execute(select(MeterReading).where(MeterReading.id == reading_id))
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

    # Предыдущие показания
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

    # Применяем ручные коррекции объема
    d_hot_final = d_hot_raw - correction_data.hot_correction
    d_cold_final = d_cold_raw - correction_data.cold_correction

    residents = Decimal(user.residents_count)
    total_residents_val = user.total_room_residents if user.total_room_residents > 0 else 1
    total_residents = Decimal(total_residents_val)

    user_share_kwh = (residents / total_residents) * d_elect_total
    d_elect_final = user_share_kwh - correction_data.electricity_correction

    vol_sewage_base = d_hot_final + d_cold_final
    vol_sewage_final = vol_sewage_base - correction_data.sewage_correction

    # Расчет стоимости
    costs = calculate_utilities(
        user=user,
        tariff=t,
        volume_hot=d_hot_final,
        volume_cold=d_cold_final,
        volume_sewage=vol_sewage_final,
        volume_electricity_share=d_elect_final
    )

    # Финансовые корректировки (Adjustments), сгруппированные по типу счета
    adj_stmt = select(
        Adjustment.account_type,
        func.sum(Adjustment.amount)
    ).where(
        Adjustment.user_id == user.id,
        Adjustment.period_id == reading.period_id
    ).group_by(Adjustment.account_type)

    adj_res = await db.execute(adj_stmt)
    adj_map = {row[0]: (row[1] or zero) for row in adj_res.all()}

    adj_209 = adj_map.get('209', zero)
    adj_205 = adj_map.get('205', zero)

    # Разделение по счетам
    cost_rent_205 = costs['cost_social_rent']
    cost_utils_209 = costs['total_cost'] - cost_rent_205

    d_209 = reading.debt_209 or zero
    o_209 = reading.overpayment_209 or zero
    d_205 = reading.debt_205 or zero
    o_205 = reading.overpayment_205 or zero

    total_209 = cost_utils_209 + d_209 - o_209 + adj_209
    total_205 = cost_rent_205 + d_205 - o_205 + adj_205
    final_total_cost = total_209 + total_205

    # Сохраняем результаты
    reading.hot_correction = correction_data.hot_correction
    reading.cold_correction = correction_data.cold_correction
    reading.electricity_correction = correction_data.electricity_correction
    reading.sewage_correction = correction_data.sewage_correction

    reading.total_209 = total_209
    reading.total_205 = total_205
    reading.total_cost = final_total_cost

    for k, v in costs.items():
        if hasattr(reading, k):
            setattr(reading, k, v)

    reading.is_approved = True

    await db.commit()

    return {"status": "approved", "new_total": final_total_cost}


# ===================================================================
# УДАЛЕНИЕ ЗАПИСИ
# ===================================================================
@router.delete("/api/admin/readings/{reading_id}")
async def delete_reading_record(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    allowed_roles = ["accountant", "admin"]
    if current_user.role not in allowed_roles:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    reading = await db.get(MeterReading, reading_id)
    if not reading:
        raise HTTPException(status_code=404, detail="Запись не найдена")

    await db.delete(reading)
    await db.commit()

    return {"status": "deleted"}