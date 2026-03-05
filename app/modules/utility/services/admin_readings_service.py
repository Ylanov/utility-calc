# app/modules/utility/services/admin_readings_service.py
from decimal import Decimal
from typing import Optional
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, asc, func, or_, update

from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment
# ИСПРАВЛЕНИЕ ЗДЕСЬ: Добавлен импорт OneTimeChargeSchema
from app.modules.utility.schemas import ApproveRequest, AdminManualReadingSchema, OneTimeChargeSchema
from app.modules.utility.services.calculations import calculate_utilities, D
from app.modules.utility.services.anomaly_detector import check_reading_for_anomalies
from app.modules.utility.constants import ANOMALY_MAP


async def get_paginated_readings(
        db: AsyncSession, page: int, limit: int, search: Optional[str],
        anomalies_only: bool, sort_by: str, sort_dir: str
):
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))).scalars().first()
    if not active_period:
        return {"total": 0, "page": page, "size": limit, "items": []}

    query = select(MeterReading, User).join(User, MeterReading.user_id == User.id).where(
        MeterReading.is_approved == False, MeterReading.period_id == active_period.id
    )

    if anomalies_only:
        query = query.where(MeterReading.anomaly_flags != None)

    if search:
        search_fmt = f"%{search}%"
        query = query.where(or_(User.username.ilike(search_fmt), User.dormitory.ilike(search_fmt)))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    sort_col = {
        "username": User.username, "dormitory": User.dormitory, "total_cost": MeterReading.total_cost
    }.get(sort_by, MeterReading.created_at)

    query = query.order_by(asc(sort_col) if sort_dir == "asc" else desc(sort_col))
    rows = (await db.execute(query.offset((page - 1) * limit).limit(limit))).all()

    if not rows:
        return {"total": total, "page": page, "size": limit, "items": []}

    user_ids = [row[1].id for row in rows]
    subq_max_prev = select(MeterReading.user_id, func.max(MeterReading.created_at).label("max_created")).where(
        MeterReading.user_id.in_(user_ids), MeterReading.is_approved == True
    ).group_by(MeterReading.user_id).subquery()

    stmt_prev = select(MeterReading).join(subq_max_prev, (MeterReading.user_id == subq_max_prev.c.user_id) & (
                MeterReading.created_at == subq_max_prev.c.max_created))
    prev_map = {r.user_id: r for r in (await db.execute(stmt_prev)).scalars().all()}

    items = []
    zero = Decimal("0.000")

    for current, user in rows:
        prev = prev_map.get(user.id)
        anomaly_details = []
        if current.anomaly_flags:
            for flag_code in current.anomaly_flags.split(','):
                details = ANOMALY_MAP.get(flag_code, ANOMALY_MAP["UNKNOWN"])
                anomaly_details.append(
                    {"code": flag_code, "message": details["message"], "severity": details["severity"]})

        items.append({
            "id": current.id, "user_id": user.id, "username": user.username, "dormitory": user.dormitory,
            "prev_hot": prev.hot_water if prev else zero, "cur_hot": current.hot_water,
            "prev_cold": prev.cold_water if prev else zero, "cur_cold": current.cold_water,
            "prev_elect": prev.electricity if prev else zero, "cur_elect": current.electricity,
            "total_cost": current.total_cost, "residents_count": user.residents_count,
            "total_room_residents": user.total_room_residents, "created_at": current.created_at,
            "anomaly_flags": current.anomaly_flags, "anomaly_details": anomaly_details,
        })

    return {"total": total, "page": page, "size": limit, "items": items}


async def bulk_approve_drafts(db: AsyncSession):
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))).scalars().first()
    if not active_period: raise HTTPException(status_code=400, detail="Нет активного периода")

    active_tariffs = (await db.execute(select(Tariff).where(Tariff.is_active == True))).scalars().all()
    if not active_tariffs: raise HTTPException(500, detail="Активные тарифы не найдены")

    tariffs_map = {t.id: t for t in active_tariffs}
    default_tariff = tariffs_map.get(1) or active_tariffs[0]

    drafts_rows = (await db.execute(select(MeterReading, User).join(User, MeterReading.user_id == User.id).where(
        MeterReading.is_approved == False, MeterReading.period_id == active_period.id))).all()

    if not drafts_rows: return {"status": "success", "approved_count": 0}

    user_ids = [row[0].user_id for row in drafts_rows]

    subq_max_date = select(MeterReading.user_id, func.max(MeterReading.created_at).label("max_created")).where(
        MeterReading.user_id.in_(user_ids), MeterReading.is_approved == True
    ).group_by(MeterReading.user_id).subquery()

    prev_readings_map = {r.user_id: r for r in (await db.execute(select(MeterReading).join(
        subq_max_date,
        (MeterReading.user_id == subq_max_date.c.user_id) & (MeterReading.created_at == subq_max_date.c.max_created)
    ))).scalars().all()}

    adj_res = await db.execute(
        select(Adjustment.user_id, Adjustment.account_type, func.sum(Adjustment.amount).label("total")).where(
            Adjustment.period_id == active_period.id, Adjustment.user_id.in_(user_ids)
        ).group_by(Adjustment.user_id, Adjustment.account_type))

    zero = Decimal("0.000")
    adj_map = {}
    for uid, acc_type, amount in adj_res.all():
        if uid not in adj_map: adj_map[uid] = {'209': zero, '205': zero}
        adj_map[uid][str(acc_type)] = amount or zero

    update_mappings = []

    for reading, user in drafts_rows:
        prev = prev_readings_map.get(reading.user_id)
        p_hot, p_cold, p_elect = D(prev.hot_water) if prev else zero, D(prev.cold_water) if prev else zero, D(
            prev.electricity) if prev else zero
        cur_hot, cur_cold, cur_elect = D(reading.hot_water), D(reading.cold_water), D(reading.electricity)

        if cur_hot < p_hot or cur_cold < p_cold or cur_elect < p_elect: continue

        user_tariff = tariffs_map.get(user.tariff_id) if getattr(user, 'tariff_id', None) else default_tariff
        user_elect_share = (Decimal(user.residents_count) / Decimal(
            user.total_room_residents if user.total_room_residents > 0 else 1)) * (cur_elect - p_elect)

        costs = calculate_utilities(
            user=user, tariff=user_tariff or default_tariff, volume_hot=cur_hot - p_hot, volume_cold=cur_cold - p_cold,
            volume_sewage=(cur_hot - p_hot) + (cur_cold - p_cold), volume_electricity_share=user_elect_share
        )

        user_adjs = adj_map.get(user.id, {'209': zero, '205': zero})
        cost_rent_205 = costs['cost_social_rent']
        cost_utils_209 = costs['total_cost'] - cost_rent_205

        total_209 = cost_utils_209 + (reading.debt_209 or zero) - (reading.overpayment_209 or zero) + user_adjs['209']
        total_205 = cost_rent_205 + (reading.debt_205 or zero) - (reading.overpayment_205 or zero) + user_adjs['205']

        update_mappings.append({
            "id": reading.id, "is_approved": True, "total_cost": total_209 + total_205,
            "total_209": total_209, "total_205": total_205, **costs
        })

    if update_mappings:
        await db.execute(update(MeterReading), update_mappings)
        await db.commit()

    return {"status": "success", "approved_count": len(update_mappings)}


async def approve_single(db: AsyncSession, reading_id: int, correction_data: ApproveRequest):
    reading = (await db.execute(select(MeterReading).where(MeterReading.id == reading_id))).scalars().first()
    if not reading: raise HTTPException(status_code=404, detail="Показания не найдены")
    if reading.is_approved: raise HTTPException(status_code=400, detail="Уже утверждены")

    user = await db.get(User, reading.user_id)
    t = (await db.execute(
        select(Tariff).where(Tariff.id == (getattr(user, 'tariff_id', None) or 1)))).scalars().first() or \
        (await db.execute(select(Tariff).where(Tariff.is_active == True))).scalars().first()

    prev = (
        await db.execute(select(MeterReading).where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
                         .order_by(MeterReading.created_at.desc()).limit(1))).scalars().first()

    zero = Decimal("0.000")
    d_hot_final = (D(reading.hot_water) - (D(prev.hot_water) if prev else zero)) - correction_data.hot_correction
    d_cold_final = (D(reading.cold_water) - (D(prev.cold_water) if prev else zero)) - correction_data.cold_correction
    d_elect_final = ((Decimal(user.residents_count) / Decimal(
        user.total_room_residents if user.total_room_residents > 0 else 1)) *
                     (D(reading.electricity) - (
                         D(prev.electricity) if prev else zero))) - correction_data.electricity_correction

    costs = calculate_utilities(user=user, tariff=t, volume_hot=d_hot_final, volume_cold=d_cold_final,
                                volume_sewage=(d_hot_final + d_cold_final) - correction_data.sewage_correction,
                                volume_electricity_share=d_elect_final)

    adj_res = await db.execute(select(Adjustment.account_type, func.sum(Adjustment.amount)).where(
        Adjustment.user_id == user.id, Adjustment.period_id == reading.period_id).group_by(Adjustment.account_type))
    adj_map = {row[0]: (row[1] or zero) for row in adj_res.all()}

    cost_rent_205 = costs['cost_social_rent']
    total_209 = (costs['total_cost'] - cost_rent_205) + (reading.debt_209 or zero) - (
                reading.overpayment_209 or zero) + adj_map.get('209', zero)
    total_205 = cost_rent_205 + (reading.debt_205 or zero) - (reading.overpayment_205 or zero) + adj_map.get('205',
                                                                                                             zero)

    reading.hot_correction, reading.cold_correction, reading.electricity_correction, reading.sewage_correction = \
        correction_data.hot_correction, correction_data.cold_correction, correction_data.electricity_correction, correction_data.sewage_correction
    reading.total_209, reading.total_205, reading.total_cost, reading.is_approved = total_209, total_205, total_209 + total_205, True

    for k, v in costs.items():
        if hasattr(reading, k): setattr(reading, k, v)

    await db.commit()
    return {"status": "approved", "new_total": total_209 + total_205}


async def get_manual_state(db: AsyncSession, user_id: int):
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))).scalars().first()
    if not active_period: raise HTTPException(status_code=400, detail="Нет активного периода")

    prev = (
        await db.execute(select(MeterReading).where(MeterReading.user_id == user_id, MeterReading.is_approved == True)
                         .order_by(MeterReading.created_at.desc()).limit(1))).scalars().first()
    draft = (
        await db.execute(select(MeterReading).where(MeterReading.user_id == user_id, MeterReading.is_approved == False,
                                                    MeterReading.period_id == active_period.id))).scalars().first()

    zero = Decimal("0.000")
    return {
        "prev_hot": prev.hot_water if prev else zero, "prev_cold": prev.cold_water if prev else zero,
        "prev_elect": prev.electricity if prev else zero,
        "draft_hot": draft.hot_water if draft else None, "draft_cold": draft.cold_water if draft else None,
        "draft_elect": draft.electricity if draft else None,
        "has_draft": bool(draft)
    }


async def save_manual_entry(db: AsyncSession, data: AdminManualReadingSchema):
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))).scalars().first()
    if not active_period: raise HTTPException(status_code=400, detail="Расчетный период закрыт.")

    user = await db.get(User, data.user_id)
    if not user or user.is_deleted: raise HTTPException(status_code=404, detail="Жилец не найден")

    t = (await db.execute(select(Tariff).where(Tariff.id == getattr(user, 'tariff_id', 1)))).scalars().first() or \
        (await db.execute(select(Tariff).where(Tariff.is_active == True))).scalars().first()

    prev = (
        await db.execute(select(MeterReading).where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
                         .order_by(MeterReading.created_at.desc()).limit(1))).scalars().first()

    zero = Decimal("0.000")
    p_hot, p_cold, p_elect = prev.hot_water if prev else zero, prev.cold_water if prev else zero, prev.electricity if prev else zero

    if data.hot_water < p_hot or data.cold_water < p_cold or data.electricity < p_elect:
        raise HTTPException(400, "Новые показания не могут быть меньше предыдущих!")

    d_hot, d_cold, d_elect = data.hot_water - p_hot, data.cold_water - p_cold, data.electricity - p_elect
    user_share_elect = (Decimal(user.residents_count) / Decimal(
        user.total_room_residents if user.total_room_residents > 0 else 1)) * d_elect

    costs = calculate_utilities(user=user, tariff=t, volume_hot=d_hot, volume_cold=d_cold, volume_sewage=d_hot + d_cold,
                                volume_electricity_share=user_share_elect)

    history = (
        await db.execute(select(MeterReading).where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
                         .order_by(MeterReading.created_at.desc()).limit(4))).scalars().all()
    anomaly_flags = check_reading_for_anomalies(
        MeterReading(hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity), history, None)

    adj_map = {row[0]: (row[1] or zero) for row in
               (await db.execute(select(Adjustment.account_type, func.sum(Adjustment.amount))
                                 .where(Adjustment.user_id == user.id,
                                        Adjustment.period_id == active_period.id).group_by(
                   Adjustment.account_type))).all()}

    draft = (
        await db.execute(select(MeterReading).where(MeterReading.user_id == user.id, MeterReading.is_approved == False,
                                                    MeterReading.period_id == active_period.id).with_for_update())).scalars().first()

    total_209 = (costs['total_cost'] - costs['cost_social_rent']) + (draft.debt_209 or zero if draft else zero) - (
        draft.overpayment_209 or zero if draft else zero) + adj_map.get('209', zero)
    total_205 = costs['cost_social_rent'] + (draft.debt_205 or zero if draft else zero) - (
        draft.overpayment_205 or zero if draft else zero) + adj_map.get('205', zero)

    if draft:
        draft.hot_water, draft.cold_water, draft.electricity, draft.anomaly_flags = data.hot_water, data.cold_water, data.electricity, anomaly_flags
        for k, v in costs.items():
            if hasattr(draft, k): setattr(draft, k, v)
        draft.total_209, draft.total_205, draft.total_cost = total_209, total_205, total_209 + total_205
    else:
        costs.pop('total_cost', None)
        db.add(MeterReading(user_id=user.id, period_id=active_period.id, hot_water=data.hot_water,
                            cold_water=data.cold_water, electricity=data.electricity,
                            debt_209=zero, overpayment_209=zero, debt_205=zero, overpayment_205=zero,
                            total_209=total_209, total_205=total_205,
                            total_cost=total_209 + total_205, is_approved=False, anomaly_flags=anomaly_flags, **costs))

    await db.commit()
    return {"status": "success"}


async def delete_reading(db: AsyncSession, reading_id: int):
    reading = await db.get(MeterReading, reading_id)
    if not reading: raise HTTPException(status_code=404, detail="Запись не найдена")
    await db.delete(reading)
    await db.commit()
    return {"status": "deleted"}


async def create_one_time_charge(db: AsyncSession, data: OneTimeChargeSchema):
    """Разовое (пропорциональное) начисление при выселении или переезде."""
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))).scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода")

    user = await db.get(User, data.user_id)
    if not user or user.is_deleted:
        raise HTTPException(status_code=404, detail="Жилец не найден")

    # Расчет доли месяца
    if data.total_days_in_month <= 0 or data.days_lived < 0 or data.days_lived > data.total_days_in_month:
        raise HTTPException(status_code=400, detail="Неверно указаны дни проживания")

    fraction = Decimal(data.days_lived) / Decimal(data.total_days_in_month)

    t = (await db.execute(select(Tariff).where(Tariff.id == getattr(user, 'tariff_id', 1)))).scalars().first() or \
        (await db.execute(select(Tariff).where(Tariff.is_active == True))).scalars().first()

    prev = (
        await db.execute(select(MeterReading).where(MeterReading.user_id == user.id, MeterReading.is_approved == True)
                         .order_by(MeterReading.created_at.desc()).limit(1))).scalars().first()

    zero = Decimal("0.000")
    p_hot, p_cold, p_elect = prev.hot_water if prev else zero, prev.cold_water if prev else zero, prev.electricity if prev else zero

    if data.hot_water < p_hot or data.cold_water < p_cold or data.electricity < p_elect:
        raise HTTPException(400, "Новые показания не могут быть меньше предыдущих!")

    d_hot, d_cold, d_elect = data.hot_water - p_hot, data.cold_water - p_cold, data.electricity - p_elect
    user_share_elect = (Decimal(user.residents_count) / Decimal(
        user.total_room_residents if user.total_room_residents > 0 else 1)) * d_elect

    # Передаем fraction для пересчета постоянных услуг!
    costs = calculate_utilities(
        user=user, tariff=t, volume_hot=d_hot, volume_cold=d_cold,
        volume_sewage=d_hot + d_cold, volume_electricity_share=user_share_elect,
        fraction=fraction
    )

    # Получаем сальдо и корректировки
    adj_map = {row[0]: (row[1] or zero) for row in
               (await db.execute(select(Adjustment.account_type, func.sum(Adjustment.amount))
                                 .where(Adjustment.user_id == user.id,
                                        Adjustment.period_id == active_period.id).group_by(
                   Adjustment.account_type))).all()}

    # Пытаемся найти черновик, чтобы перезаписать его, либо создаем новую запись
    draft = (
        await db.execute(select(MeterReading).where(MeterReading.user_id == user.id, MeterReading.is_approved == False,
                                                    MeterReading.period_id == active_period.id).with_for_update())).scalars().first()

    total_209 = (costs['total_cost'] - costs['cost_social_rent']) + (draft.debt_209 or zero if draft else zero) - (
        draft.overpayment_209 or zero if draft else zero) + adj_map.get('209', zero)
    total_205 = costs['cost_social_rent'] + (draft.debt_205 or zero if draft else zero) - (
        draft.overpayment_205 or zero if draft else zero) + adj_map.get('205', zero)

    anomaly_flags = "ONE_TIME_CHARGE"  # Специальная пометка

    if draft:
        draft.hot_water, draft.cold_water, draft.electricity, draft.anomaly_flags = data.hot_water, data.cold_water, data.electricity, anomaly_flags
        for k, v in costs.items(): getattr(draft, k)  # trick to avoid syntax error
        for k, v in costs.items(): setattr(draft, k, v)
        draft.total_209, draft.total_205, draft.total_cost = total_209, total_205, total_209 + total_205
        # ВАЖНО: Разовое начисление сразу утверждается (is_approved=True), чтобы квитанция сформировалась немедленно
        draft.is_approved = True
    else:
        costs.pop('total_cost', None)
        db.add(MeterReading(user_id=user.id, period_id=active_period.id, hot_water=data.hot_water,
                            cold_water=data.cold_water, electricity=data.electricity,
                            debt_209=zero, overpayment_209=zero, debt_205=zero, overpayment_205=zero,
                            total_209=total_209, total_205=total_205,
                            total_cost=total_209 + total_205, is_approved=True, anomaly_flags=anomaly_flags, **costs))

    # Логика выселения
    if data.is_moving_out:
        user.is_deleted = True
        user.username = f"{user.username}_deleted_{user.id}"

    await db.commit()
    return {"status": "success"}