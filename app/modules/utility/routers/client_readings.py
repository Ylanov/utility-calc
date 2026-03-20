import os
import uuid
import asyncio
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import func

from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment
from app.modules.utility.schemas import ReadingSchema, ReadingStateResponse
from app.core.dependencies import get_current_user
from app.modules.utility.services.calculations import calculate_utilities
from app.modules.utility.services.anomaly_detector import check_reading_for_anomalies
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.s3_client import s3_service

router = APIRouter(tags=["Client Readings"])


# =========================
# SERVICE LAYER
# =========================
class ReadingService:

    @staticmethod
    def parse_input(data: ReadingSchema):
        try:
            return (
                Decimal(str(data.hot_water)),
                Decimal(str(data.cold_water)),
                Decimal(str(data.electricity))
            )
        except Exception:
            raise HTTPException(400, "Некорректный формат данных")

    @staticmethod
    def validate(hot, cold, elect, prev):
        zero = Decimal("0.000")
        p_hot = prev.hot_water if prev and prev.hot_water is not None else zero
        p_cold = prev.cold_water if prev and prev.cold_water is not None else zero
        p_elect = prev.electricity if prev and prev.electricity is not None else zero

        if hot < p_hot or cold < p_cold or elect < p_elect:
            raise HTTPException(400, "Новые показания не могут быть меньше предыдущих")

        return p_hot, p_cold, p_elect

    @staticmethod
    def calculate_costs(user: User, tariff: Tariff, hot, cold, elect, p_hot, p_cold, p_elect):
        d_hot = hot - p_hot
        d_cold = cold - p_cold
        d_elect = elect - p_elect
        sewage = d_hot + d_cold

        residents = Decimal(user.residents_count or 1)
        total = Decimal(user.total_room_residents or 1)
        if total == 0: total = Decimal("1")
        elect_share = (residents / total) * d_elect

        return calculate_utilities(
            user=user,
            tariff=tariff,
            volume_hot=d_hot,
            volume_cold=d_cold,
            volume_sewage=sewage,
            volume_electricity_share=elect_share
        )


# =========================
# STATE
# =========================
@router.get("/api/readings/state", response_model=ReadingStateResponse)
async def get_reading_state(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))).scalars().first()

    readings = (await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == current_user.id)
        .order_by(MeterReading.created_at.desc())
        .limit(2)
    )).scalars().all()

    prev = next((r for r in readings if r.is_approved), None)
    draft = next((r for r in readings if not r.is_approved), None)

    zero = Decimal("0.000")

    # ВАЖНО: Возвращаем все поля, чтобы UI веб-версии не сломался!
    return {
        "period_name": period.name if period else "Период закрыт",
        "prev_hot": prev.hot_water if prev else zero,
        "prev_cold": prev.cold_water if prev else zero,
        "prev_elect": prev.electricity if prev else zero,

        "current_hot": draft.hot_water if draft else None,
        "current_cold": draft.cold_water if draft else None,
        "current_elect": draft.electricity if draft else None,

        "total_cost": draft.total_cost if draft else None,
        "total_209": draft.total_209 if draft else None,
        "total_205": draft.total_205 if draft else None,

        "is_draft": bool(draft),
        "is_period_open": bool(period),

        "cost_hot_water": draft.cost_hot_water if draft else None,
        "cost_cold_water": draft.cost_cold_water if draft else None,
        "cost_electricity": draft.cost_electricity if draft else None,
        "cost_sewage": draft.cost_sewage if draft else None,
        "cost_maintenance": draft.cost_maintenance if draft else None,
        "cost_social_rent": draft.cost_social_rent if draft else None,
        "cost_waste": draft.cost_waste if draft else None,
        "cost_fixed_part": draft.cost_fixed_part if draft else None,
    }


# =========================
# CALCULATE
# =========================
@router.post("/api/calculate")
async def save_reading(
        data: ReadingSchema,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    hot, cold, elect = ReadingService.parse_input(data)

    period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))).scalars().first()
    if not period:
        raise HTTPException(400, "Расчетный период закрыт")

    tariff_id = getattr(current_user, 'tariff_id', None) or 1
    tariff = (await db.execute(select(Tariff).where(Tariff.id == tariff_id))).scalars().first()
    if not tariff:
        tariff = (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()
        if not tariff:
            raise HTTPException(500, "Тариф не найден")

    # История для аномалий и предыдущих показаний
    readings = (await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved)
        .order_by(MeterReading.created_at.desc())
        .limit(4)
    )).scalars().all()

    prev = readings[0] if readings else None
    p_hot, p_cold, p_elect = ReadingService.validate(hot, cold, elect, prev)

    # Высчитываем базовые стоимости
    costs = ReadingService.calculate_costs(current_user, tariff, hot, cold, elect, p_hot, p_cold, p_elect)

    # Получаем корректировки
    adj = (await db.execute(
        select(Adjustment.account_type, func.sum(Adjustment.amount))
        .where(Adjustment.user_id == current_user.id, Adjustment.period_id == period.id)
        .group_by(Adjustment.account_type)
    )).all()
    adj_map = {a[0]: (a[1] or Decimal("0.00")) for a in adj}

    # Считаем аномалии синхронно (как и должно быть)
    temp_reading = MeterReading(hot_water=hot, cold_water=cold, electricity=elect)
    anomaly_flags = check_reading_for_anomalies(temp_reading, readings, None)

    async with db.begin():
        draft = (await db.execute(
            select(MeterReading)
            .where(
                MeterReading.user_id == current_user.id,
                MeterReading.is_approved.is_(False),
                MeterReading.period_id == period.id
            )
            .with_for_update()
        )).scalars().first()

        # ВАЖНО: Учитываем старые долги из БД!
        d_209 = draft.debt_209 or Decimal("0.00") if draft else Decimal("0.00")
        o_209 = draft.overpayment_209 or Decimal("0.00") if draft else Decimal("0.00")
        d_205 = draft.debt_205 or Decimal("0.00") if draft else Decimal("0.00")
        o_205 = draft.overpayment_205 or Decimal("0.00") if draft else Decimal("0.00")

        cost_rent = costs['cost_social_rent']
        cost_utils = costs['total_cost'] - cost_rent

        total_209 = cost_utils + d_209 - o_209 + adj_map.get('209', Decimal("0.00"))
        total_205 = cost_rent + d_205 - o_205 + adj_map.get('205', Decimal("0.00"))
        grand_total = total_209 + total_205

        if draft:
            draft.hot_water = hot
            draft.cold_water = cold
            draft.electricity = elect
            draft.anomaly_flags = anomaly_flags
            draft.total_209 = total_209
            draft.total_205 = total_205
            draft.total_cost = grand_total
            for k, v in costs.items():
                if hasattr(draft, k):
                    setattr(draft, k, v)
        else:
            costs_for_create = costs.copy()
            costs_for_create.pop('total_cost', None)

            db.add(MeterReading(
                user_id=current_user.id,
                period_id=period.id,
                hot_water=hot,
                cold_water=cold,
                electricity=elect,
                debt_209=Decimal("0.00"), overpayment_209=Decimal("0.00"),
                debt_205=Decimal("0.00"), overpayment_205=Decimal("0.00"),
                total_209=total_209,
                total_205=total_205,
                total_cost=grand_total,
                is_approved=False,
                anomaly_flags=anomaly_flags,
                **costs_for_create  # ВАЖНО: сохраняем детализацию стоимости!
            ))

    return {
        "status": "success",
        "total_cost": grand_total,
        "total_209": total_209,
        "total_205": total_205
    }


# =========================
# HISTORY
# =========================
@router.get("/api/readings/history")
async def get_client_history(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    readings = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.period))
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved)
        .order_by(MeterReading.created_at.desc())
        .limit(24)
    )).scalars().all()

    return [
        {
            "id": r.id,
            "period": r.period.name if r.period else "Неизвестно",
            "hot": r.hot_water,
            "cold": r.cold_water,
            "electric": r.electricity,
            "total": r.total_cost,
            "date": r.created_at
        }
        for r in readings
    ]


# =========================
# RECEIPT
# =========================
@router.get("/api/client/receipts/{reading_id}")
async def download_client_receipt(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    reading = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.user), selectinload(MeterReading.period))
        .where(MeterReading.id == reading_id)
    )).scalars().first()

    if not reading or reading.user_id != current_user.id:
        raise HTTPException(404, "Квитанция не найдена")
    if not reading.is_approved:
        raise HTTPException(400, "Квитанция еще не сформирована")

    tariff = (await db.execute(
        select(Tariff).where(Tariff.id == (reading.user.tariff_id or 1))
    )).scalars().first()

    # ВАЖНО: Получаем предыдущие показания и корректировки, чтобы квитанция была полная
    prev = (await db.execute(
        select(MeterReading)
        .where(MeterReading.user_id == reading.user_id, MeterReading.is_approved,
               MeterReading.created_at < reading.created_at)
        .order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    adjustments = (await db.execute(
        select(Adjustment).where(Adjustment.user_id == reading.user_id, Adjustment.period_id == reading.period_id)
    )).scalars().all()

    pdf = await asyncio.to_thread(generate_receipt_pdf,
                                  user=reading.user,
                                  reading=reading,
                                  period=reading.period,
                                  tariff=tariff,
                                  prev_reading=prev,
                                  adjustments=adjustments,
                                  output_dir="/tmp"
                                  )

    key = f"receipts/{reading.period.id}/client_view_{reading.user.id}_{uuid.uuid4().hex[:8]}.pdf"

    upload_success = await asyncio.to_thread(s3_service.upload_file, pdf, key)
    if upload_success:
        await asyncio.to_thread(os.remove, pdf)
        url = await asyncio.to_thread(s3_service.get_presigned_url, key, 300)
        return RedirectResponse(url=url)
    else:
        raise HTTPException(500, "Ошибка генерации файла")