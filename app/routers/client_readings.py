from typing import List, Optional
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
async def get_reading_state(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """Получение текущего состояния для Dashboard жильца."""
    period_res = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = period_res.scalars().first()

    prev_res = await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )
    prev = prev_res.scalars().first()

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

    return {
        "period_name": active_period.name if active_period else "Прием показаний закрыт",
        "prev_hot": prev.hot_water if prev else zero_vol,
        "prev_cold": prev.cold_water if prev else zero_vol,
        "prev_elect": prev.electricity if prev else zero_vol,
        "current_hot": draft.hot_water if draft else None,
        "current_cold": draft.cold_water if draft else None,
        "current_elect": draft.electricity if draft else None,
        "total_cost": draft.total_cost if draft else None,
        "total_209": draft.total_209 if draft else None,
        "total_205": draft.total_205 if draft else None,
        "is_draft": True if draft else False,
        "is_period_open": True if active_period else False,
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
    """Расчет и сохранение показаний (Черновик) с поддержкой раздельного учета."""
    res_period = await db.execute(select(BillingPeriod).where(BillingPeriod.is_active == True))
    active_period = res_period.scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Расчетный период закрыт.")

    t_res = await db.execute(select(Tariff).where(Tariff.is_active == True))
    t = t_res.scalars().first()
    if not t:
        raise HTTPException(status_code=500, detail="Активный тариф не найден")

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

    if data.hot_water < p_hot or data.cold_water < p_cold or data.electricity < p_elect:
        raise HTTPException(400, "Новые показания не могут быть меньше предыдущих.")

    d_hot = data.hot_water - p_hot
    d_cold = data.cold_water - p_cold
    d_elect_total = data.electricity - p_elect
    residents = Decimal(current_user.residents_count)
    total_residents = Decimal(current_user.total_room_residents if current_user.total_room_residents > 0 else 1)
    user_share_kwh = (residents / total_residents) * d_elect_total
    vol_sewage = d_hot + d_cold

    costs = calculate_utilities(
        user=current_user, tariff=t, volume_hot=d_hot, volume_cold=d_cold,
        volume_sewage=vol_sewage, volume_electricity_share=user_share_kwh
    )
    cost_rent_205 = costs['cost_social_rent']
    cost_utils_209 = costs['total_cost'] - cost_rent_205

    history_res = await db.execute(select(MeterReading).where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True).order_by(MeterReading.created_at.desc()).limit(4))
    history = history_res.scalars().all()
    temp_reading = MeterReading(hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity)
    anomaly_flags = check_reading_for_anomalies(temp_reading, history, None)

    draft_res = await db.execute(select(MeterReading).where(MeterReading.user_id == current_user.id, MeterReading.is_approved == False, MeterReading.period_id == active_period.id).with_for_update())
    draft = draft_res.scalars().first()

    adj_stmt = (select(Adjustment.account_type, func.sum(Adjustment.amount)).where(Adjustment.user_id == current_user.id, Adjustment.period_id == active_period.id).group_by(Adjustment.account_type))
    adj_res = await db.execute(adj_stmt)
    adj_map = {row[0]: (row[1] or Decimal("0.00")) for row in adj_res.all()}
    adj_209 = adj_map.get('209', Decimal("0.00"))
    adj_205 = adj_map.get('205', Decimal("0.00"))

    d_209 = draft.debt_209 or Decimal("0.00") if draft else Decimal("0.00")
    o_209 = draft.overpayment_209 or Decimal("0.00") if draft else Decimal("0.00")
    d_205 = draft.debt_205 or Decimal("0.00") if draft else Decimal("0.00")
    o_205 = draft.overpayment_205 or Decimal("0.00") if draft else Decimal("0.00")

    total_209 = cost_utils_209 + d_209 - o_209 + adj_209
    total_205 = cost_rent_205 + d_205 - o_205 + adj_205
    grand_total = total_209 + total_205

    if draft:
        draft.hot_water, draft.cold_water, draft.electricity = data.hot_water, data.cold_water, data.electricity
        for k, v in costs.items():
            if hasattr(draft, k):
                setattr(draft, k, v)
        draft.total_209, draft.total_205, draft.total_cost = total_209, total_205, grand_total
        draft.anomaly_flags = anomaly_flags
    else:
        # Исправлено: Удаляем дублирующийся ключ перед распаковкой
        costs.pop('total_cost', None)
        new_reading = MeterReading(
            user_id=current_user.id, period_id=active_period.id,
            hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity,
            debt_209=Decimal("0.00"), overpayment_209=Decimal("0.00"),
            debt_205=Decimal("0.00"), overpayment_205=Decimal("0.00"),
            **costs,
            total_209=total_209, total_205=total_205, total_cost=grand_total,
            is_approved=False, anomaly_flags=anomaly_flags
        )
        db.add(new_reading)

    await db.commit()

    return {"status": "success", "total_cost": grand_total, "total_209": total_209, "total_205": total_205}

@router.get("/api/readings/history")
async def get_client_history(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    stmt = (
        select(MeterReading)
        .options(selectinload(MeterReading.period))
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc())
        .limit(24) # Ограничиваем историю
    )
    result = await db.execute(stmt)
    readings = result.scalars().all()
    history = []
    for r in readings:
        history.append({
            "id": r.id, "period": r.period.name if r.period else "Неизвестно",
            "hot": r.hot_water, "cold": r.cold_water, "electric": r.electricity,
            "total": r.total_cost, "date": r.created_at
        })
    return history

@router.get("/api/client/receipts/{reading_id}")
async def download_client_receipt(
    reading_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = (
        select(MeterReading)
        .options(selectinload(MeterReading.user), selectinload(MeterReading.period))
        .where(MeterReading.id == reading_id)
    )
    res = await db.execute(stmt)
    reading = res.scalars().first()

    if not reading: raise HTTPException(404, "Квитанция не найдена")
    if reading.user_id != current_user.id: raise HTTPException(403, "Это не ваша квитанция")
    if not reading.is_approved: raise HTTPException(400, "Квитанция еще не сформирована")

    tariff_res = await db.execute(select(Tariff).where(Tariff.is_active == True))
    tariff = tariff_res.scalars().first()
    if not tariff: raise HTTPException(500, "Активный тариф не найден")

    prev_stmt = (
        select(MeterReading)
        .where(MeterReading.user_id == reading.user_id, MeterReading.is_approved == True, MeterReading.created_at < reading.created_at)
        .order_by(MeterReading.created_at.desc()).limit(1)
    )
    prev_res = await db.execute(prev_stmt)
    prev = prev_res.scalars().first()

    adj_stmt = select(Adjustment).where(Adjustment.user_id == reading.user_id, Adjustment.period_id == reading.period_id)
    adj_res = await db.execute(adj_stmt)
    adjustments = adj_res.scalars().all()

    try:
        pdf_path = generate_receipt_pdf(
            user=reading.user, reading=reading, period=reading.period,
            tariff=tariff, prev_reading=prev, adjustments=adjustments
        )
        filename = f"receipt_{reading.period.name}.pdf"
        return FileResponse(path=pdf_path, filename=filename, media_type="application/pdf")
    except Exception as e:
        print(f"Error generating PDF for client: {e}")
        raise HTTPException(500, "Ошибка формирования файла")