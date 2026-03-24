# app/modules/utility/routers/client_readings.py

import os
import uuid
import asyncio
from decimal import Decimal
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import func

from app.core.database import get_db
# ИМПОРТ: Добавляем модель Room
from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment, Room
from app.modules.utility.schemas import ReadingSchema, ReadingStateResponse
from app.core.dependencies import get_current_user
from app.modules.utility.services.calculations import calculate_utilities
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.s3_client import s3_service
from app.modules.utility.tasks import detect_anomalies_task

router = APIRouter(tags=["Client Readings"])


# =========================
# SERVICE LAYER (Адаптирован под Room)
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
    def calculate_costs(user: User, room: Room, tariff: Tariff, hot, cold, elect, p_hot, p_cold, p_elect):
        d_hot = hot - p_hot
        d_cold = cold - p_cold
        d_elect = elect - p_elect
        sewage = d_hot + d_cold

        # Данные о количестве жильцов берем от пользователя (кто платит)
        residents = Decimal(user.residents_count or 1)
        # А данные о вместимости - из комнаты
        total = Decimal(room.total_room_residents or 1)
        if total == 0: total = Decimal("1")
        elect_share = (residents / total) * d_elect

        # В calculate_utilities передаем и user, и room, чтобы у сервиса были все данные
        return calculate_utilities(
            user=user,
            room=room, # Передаем объект комнаты
            tariff=tariff,
            volume_hot=d_hot,
            volume_cold=d_cold,
            volume_sewage=sewage,
            volume_electricity_share=elect_share
        )


# =========================
# STATE (Адаптирован под Room)
# =========================
@router.get("/api/readings/state", response_model=ReadingStateResponse)
async def get_reading_state(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    # Загружаем пользователя вместе с его комнатой, чтобы избежать лишних запросов
    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == current_user.id)
    )).scalars().first()

    if not user or not user.room_id:
        raise HTTPException(status_code=400, detail="Вы не привязаны к помещению. Обратитесь к администратору.")

    period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))).scalars().first()

    # ИЗМЕНЕНИЕ: Ищем историю показаний для КОМНАТЫ, а не для пользователя
    readings = (await db.execute(
        select(MeterReading)
        .where(MeterReading.room_id == user.room_id)
        .order_by(MeterReading.created_at.desc())
        .limit(3)
    )).scalars().all()

    # 🔥 ИСПРАВЛЕНИЕ: Ищем показание за ТЕКУЩИЙ активный период ДЛЯ КОМНАТЫ (неважно, кто из соседей подал)
    current_reading = next((r for r in readings if period and r.period_id == period.id), None)

    # Ищем последнее утвержденное показание из ПРОШЛЫХ периодов ДЛЯ КОМНАТЫ
    prev = next((r for r in readings if r.is_approved and (not period or r.period_id != period.id)), None)

    is_already_approved = current_reading.is_approved if current_reading else False
    is_draft = current_reading is not None and not current_reading.is_approved

    zero = Decimal("0.000")

    return {
        "period_name": period.name if period else "Период закрыт",
        "prev_hot": prev.hot_water if prev else zero,
        "prev_cold": prev.cold_water if prev else zero,
        "prev_elect": prev.electricity if prev else zero,

        "current_hot": current_reading.hot_water if current_reading else None,
        "current_cold": current_reading.cold_water if current_reading else None,
        "current_elect": current_reading.electricity if current_reading else None,

        "total_cost": current_reading.total_cost if current_reading else None,
        "total_209": current_reading.total_209 if current_reading else None,
        "total_205": current_reading.total_205 if current_reading else None,

        "is_draft": is_draft,
        "is_period_open": bool(period),
        "is_already_approved": is_already_approved,

        "cost_hot_water": current_reading.cost_hot_water if current_reading else None,
        "cost_cold_water": current_reading.cost_cold_water if current_reading else None,
        "cost_electricity": current_reading.cost_electricity if current_reading else None,
        "cost_sewage": current_reading.cost_sewage if current_reading else None,
        "cost_maintenance": current_reading.cost_maintenance if current_reading else None,
        "cost_social_rent": current_reading.cost_social_rent if current_reading else None,
        "cost_waste": current_reading.cost_waste if current_reading else None,
        "cost_fixed_part": current_reading.cost_fixed_part if current_reading else None,
    }


# =========================
# CALCULATE (Адаптирован под Room)
# =========================
@router.post("/api/calculate")
async def save_reading(
        data: ReadingSchema,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    hot, cold, elect = ReadingService.parse_input(data)

    # Загружаем пользователя вместе с его комнатой
    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == current_user.id)
    )).scalars().first()

    if not user or not user.room_id:
        raise HTTPException(status_code=400, detail="Вы не привязаны к помещению для подачи показаний.")

    room = user.room

    # 1. ПАРАЛЛЕЛЬНЫЕ ЗАПРОСЫ
    period_task = db.execute(select(BillingPeriod).where(BillingPeriod.is_active))
    tariff_task = db.execute(select(Tariff).where(Tariff.id == (getattr(user, 'tariff_id', None) or 1)))

    period_res, tariff_res = await asyncio.gather(period_task, tariff_task)

    period = period_res.scalars().first()
    if not period:
        raise HTTPException(400, "Расчетный период закрыт")

    tariff = tariff_res.scalars().first()
    if not tariff:
        tariff = (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()
        if not tariff:
            raise HTTPException(500, "Тариф не найден")

    # 2. ИЗМЕНЕНИЕ: Ищем историю показаний для КОМНАТЫ
    history_task = db.execute(
        select(MeterReading)
        .where(MeterReading.room_id == user.room_id)
        .order_by(MeterReading.created_at.desc())
        .limit(6)
    )
    # Корректировки остаются привязанными к плательщику (user.id)
    adj_task = db.execute(
        select(Adjustment.account_type, func.sum(Adjustment.amount))
        .where(Adjustment.user_id == user.id, Adjustment.period_id == period.id)
        .group_by(Adjustment.account_type)
    )

    history_res, adj_res = await asyncio.gather(history_task, adj_task)

    readings = history_res.scalars().all()
    adj_map = {a[0]: (a[1] or Decimal("0.00")) for a in adj_res.all()}

    # 🔥 ИСПРАВЛЕНИЕ: Ищем черновик КОМНАТЫ в текущем периоде
    draft = next((r for r in readings if r.period_id == period.id), None)

    # 🔥 ЗАЩИТА: Если черновик есть и его создал сосед, блокируем перезапись
    if draft and draft.user_id != user.id:
        raise HTTPException(status_code=400, detail="Показания для вашей комнаты уже переданы другим жильцом.")

    # Ищем последнее утвержденное показание КОМНАТЫ
    prev_latest = next((r for r in readings if r.is_approved and r.period_id != period.id), None)
    prev_manual = next(
        (r for r in readings if r.is_approved and r.period_id != period.id and r.anomaly_flags != "AUTO_GENERATED"),
        None)

    zero = Decimal("0.000")

    p_hot_man = prev_manual.hot_water if prev_manual and prev_manual.hot_water is not None else zero
    p_cold_man = prev_manual.cold_water if prev_manual and prev_manual.cold_water is not None else zero
    p_elect_man = prev_manual.electricity if prev_manual and prev_manual.electricity is not None else zero

    if hot < p_hot_man or cold < p_cold_man or elect < p_elect_man:
        raise HTTPException(400, "Новые показания не могут быть меньше последних показаний по этому помещению.")

    p_hot = prev_latest.hot_water if prev_latest else zero
    p_cold = prev_latest.cold_water if prev_latest else zero
    p_elect = prev_latest.electricity if prev_latest else zero

    # 3. ИЗМЕНЕНИЕ: Передаем в расчет и user, и room
    costs = ReadingService.calculate_costs(user, room, tariff, hot, cold, elect, p_hot, p_cold, p_elect)

    # 4. Сборка долгов и итогов
    d_209 = draft.debt_209 or Decimal("0.00") if draft else Decimal("0.00")
    o_209 = draft.overpayment_209 or Decimal("0.00") if draft else Decimal("0.00")
    d_205 = draft.debt_205 or Decimal("0.00") if draft else Decimal("0.00")
    o_205 = draft.overpayment_205 or Decimal("0.00") if draft else Decimal("0.00")

    cost_rent = costs['cost_social_rent']
    cost_utils = costs['total_cost'] - cost_rent

    total_209 = cost_utils + d_209 - o_209 + adj_map.get('209', Decimal("0.00"))
    total_205 = cost_rent + d_205 - o_205 + adj_map.get('205', Decimal("0.00"))
    grand_total = total_209 + total_205

    # 5. СОХРАНЕНИЕ
    if draft:
        if draft.is_approved:
            raise HTTPException(400, "Ваши показания уже проверены и приняты бухгалтерией. Изменение невозможно.")

        old_record = {
            "hot": str(draft.hot_water), "cold": str(draft.cold_water), "elect": str(draft.electricity),
            "date": datetime.utcnow().strftime("%d.%m.%Y %H:%M")
        }
        history_list = draft.edit_history if draft.edit_history else[]
        draft.edit_history = history_list + [old_record]
        draft.edit_count = (draft.edit_count or 0) + 1

        draft.hot_water, draft.cold_water, draft.electricity = hot, cold, elect
        draft.total_209, draft.total_205, draft.total_cost = total_209, total_205, grand_total
        draft.anomaly_flags, draft.anomaly_score = "PENDING", 0

        for key, value in costs.items():
            if hasattr(draft, key):
                setattr(draft, key, value)

        db.add(draft)
        await db.flush()
        reading_id_for_celery = draft.id
    else:
        costs_for_create = costs.copy()
        costs_for_create.pop('total_cost', None)

        new_draft = MeterReading(
            user_id=user.id,
            room_id=user.room_id, # <--- ОБЯЗАТЕЛЬНОЕ ПОЛЕ
            period_id=period.id,
            hot_water=hot, cold_water=cold, electricity=elect,
            debt_209=Decimal("0.00"), overpayment_209=Decimal("0.00"),
            debt_205=Decimal("0.00"), overpayment_205=Decimal("0.00"),
            total_209=total_209, total_205=total_205, total_cost=grand_total,
            is_approved=False, anomaly_flags="PENDING", anomaly_score=0,
            edit_count=1, edit_history=[],
            **costs_for_create
        )
        db.add(new_draft)
        await db.flush()
        reading_id_for_celery = new_draft.id

    await db.commit()

    # 6. Запускаем асинхронную проверку на аномалии
    detect_anomalies_task.delay(reading_id_for_celery)

    return {"status": "success", "total_cost": grand_total, "total_209": total_209, "total_205": total_205}


# =========================
# HISTORY
# =========================
@router.get("/api/readings/history")
async def get_client_history(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # История показывает только те квитанции, которые были выставлены на этого пользователя
    readings = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.period))
        .where(MeterReading.user_id == current_user.id, MeterReading.is_approved)
        .order_by(MeterReading.created_at.desc())
        .limit(24)
    )).scalars().all()

    return[{"id": r.id, "period": r.period.name if r.period else "Неизвестно", "hot": r.hot_water,
             "cold": r.cold_water, "electric": r.electricity, "total": r.total_cost, "date": r.created_at}
            for r in readings]


# =========================
# RECEIPT (Адаптирован под Room)
# =========================
@router.get("/api/client/receipts/{reading_id}")
async def download_client_receipt(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    # 1. Получаем показание со связями User и Period
    reading = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.user), selectinload(MeterReading.period))
        .where(MeterReading.id == reading_id)
    )).scalars().first()

    # 2. Проверки доступа
    if not reading or reading.user_id != current_user.id:
        raise HTTPException(404, "Квитанция не найдена")

    if not reading.is_approved:
        raise HTTPException(400, "Квитанция еще не сформирована")

    # 3. Получаем тариф
    tariff = (await db.execute(select(Tariff).where(Tariff.is_active).order_by(Tariff.valid_from.desc()))).scalars().first()
    if not tariff: raise HTTPException(500, "Тариф не найден")

    # 4. ИЗМЕНЕНИЕ: Ищем предыдущее показание для КОМНАТЫ
    prev = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.room_id == reading.room_id, # <--- Ключевое изменение
            MeterReading.is_approved,
            MeterReading.created_at < reading.created_at
        )
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )).scalars().first()

    # 5. Корректировки (остаются привязанными к пользователю)
    adjustments = (await db.execute(
        select(Adjustment).where(Adjustment.user_id == reading.user_id, Adjustment.period_id == reading.period_id)
    )).scalars().all()

    # 6. Генерация PDF
    pdf_path = await asyncio.to_thread(
        generate_receipt_pdf,
        user=reading.user, reading=reading, period=reading.period, tariff=tariff,
        prev_reading=prev, adjustments=adjustments, output_dir="/tmp"
    )

    # 7. Загрузка в S3
    period_id = reading.period.id if reading.period else "unknown"
    key = f"receipts/{period_id}/client_view_{reading.user.id}_{uuid.uuid4().hex[:8]}.pdf"

    upload_success = await asyncio.to_thread(s3_service.upload_file, pdf_path, key)
    if not upload_success:
        raise HTTPException(500, "Ошибка генерации файла")

    await asyncio.to_thread(os.remove, pdf_path)
    url = await asyncio.to_thread(s3_service.get_presigned_url, key, 300)

    return {"status": "success", "url": url}