# app/modules/utility/routers/client_readings.py

import asyncio
import logging
import os
from decimal import Decimal
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import func

from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment, Room
from app.modules.utility.schemas import ReadingSchema, ReadingStateResponse
from app.core.dependencies import get_current_user
from app.modules.utility.services.calculations import calculate_utilities
from app.modules.utility.services.pdf_generator import generate_receipt_pdf
from app.modules.utility.services.s3_client import s3_service
from app.modules.utility.tasks import detect_anomalies_task

router = APIRouter(tags=["Client Readings"])
logger = logging.getLogger(__name__)


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
    def calculate_costs(user: User, room: Room, tariff: Tariff, hot, cold, elect, p_hot, p_cold, p_elect):
        d_hot = hot - p_hot
        d_cold = cold - p_cold
        d_elect = elect - p_elect
        sewage = d_hot + d_cold

        residents = Decimal(user.residents_count or 1)
        total = Decimal(room.total_room_residents or 1)
        if total == 0:
            total = Decimal("1")
        elect_share = (residents / total) * d_elect

        return calculate_utilities(
            user=user,
            room=room,
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
    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == current_user.id)
    )).scalars().first()

    if not user or not user.room_id:
        raise HTTPException(status_code=400, detail="Вы не привязаны к помещению. Обратитесь к администратору.")

    period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active)
    )).scalars().first()

    is_period_open = period is not None

    # История показаний комнаты
    readings = (await db.execute(
        select(MeterReading)
        .where(MeterReading.room_id == user.room_id)
        .order_by(MeterReading.created_at.desc())
        .limit(12)
    )).scalars().all()

    zero = Decimal("0.000")

    # Последнее утверждённое показание комнаты (для отображения предыдущих значений)
    prev_latest = next((r for r in readings if r.is_approved), None)
    prev_hot = prev_latest.hot_water if prev_latest else zero
    prev_cold = prev_latest.cold_water if prev_latest else zero
    prev_elect = prev_latest.electricity if prev_latest else zero

    # Черновик текущего периода
    current_reading = None
    is_draft = False
    is_already_approved = False

    if period:
        current_reading = next((r for r in readings if r.period_id == period.id), None)
        if current_reading:
            is_draft = not current_reading.is_approved
            is_already_approved = current_reading.is_approved

    return {
        "period_name": period.name if period else None,
        "prev_hot": prev_hot,
        "prev_cold": prev_cold,
        "prev_elect": prev_elect,
        "current_hot": current_reading.hot_water if current_reading else None,
        "current_cold": current_reading.cold_water if current_reading else None,
        "current_elect": current_reading.electricity if current_reading else None,
        "total_cost": current_reading.total_cost if current_reading else None,
        "total_209": current_reading.total_209 if current_reading else None,
        "total_205": current_reading.total_205 if current_reading else None,
        "is_draft": is_draft,
        "is_period_open": is_period_open,
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
# CALCULATE
# =========================
@router.post("/api/calculate")
async def save_reading(
        data: ReadingSchema,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    hot, cold, elect = ReadingService.parse_input(data)

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

    # 2. ИСПРАВЛЕНИЕ race condition: используем SELECT FOR UPDATE чтобы заблокировать
    # черновик на время транзакции. Два соседа не смогут одновременно создать дубль.
    draft_result = await db.execute(
        select(MeterReading)
        .where(
            MeterReading.room_id == user.room_id,
            MeterReading.period_id == period.id,
            MeterReading.is_approved.is_(False)
        )
        .with_for_update()  # блокировка строки на время транзакции
    )
    draft = draft_result.scalars().first()

    # Если черновик создал сосед — блокируем перезапись
    if draft and draft.user_id != user.id:
        raise HTTPException(
            status_code=400,
            detail="Показания для вашей комнаты уже переданы другим жильцом."
        )

    # 3. История показаний КОМНАТЫ (для расчёта расхода)
    history_task = db.execute(
        select(MeterReading)
        .where(MeterReading.room_id == user.room_id)
        .order_by(MeterReading.created_at.desc())
        .limit(12)
    )
    adj_task = db.execute(
        select(Adjustment.account_type, func.sum(Adjustment.amount))
        .where(Adjustment.user_id == user.id, Adjustment.period_id == period.id)
        .group_by(Adjustment.account_type)
    )

    history_res, adj_res = await asyncio.gather(history_task, adj_task)

    readings = history_res.scalars().all()
    adj_map = {a[0]: (a[1] or Decimal("0.00")) for a in adj_res.all()}

    # 4. Предыдущие реальные показания (не авто-сгенерированные)
    prev_latest = next((r for r in readings if r.is_approved and r.period_id != period.id), None)
    prev_manual = next(
        (r for r in readings if r.is_approved and r.period_id != period.id and r.anomaly_flags != "AUTO_GENERATED"),
        None
    )

    zero = Decimal("0.000")

    p_hot_man = prev_manual.hot_water if prev_manual and prev_manual.hot_water is not None else zero
    p_cold_man = prev_manual.cold_water if prev_manual and prev_manual.cold_water is not None else zero
    p_elect_man = prev_manual.electricity if prev_manual and prev_manual.electricity is not None else zero

    if hot < p_hot_man or cold < p_cold_man or elect < p_elect_man:
        raise HTTPException(400, "Новые показания не могут быть меньше последних показаний по этому помещению.")

    p_hot = prev_latest.hot_water if prev_latest else zero
    p_cold = prev_latest.cold_water if prev_latest else zero
    p_elect = prev_latest.electricity if prev_latest else zero

    # 5. Расчёт стоимостей
    costs = ReadingService.calculate_costs(user, room, tariff, hot, cold, elect, p_hot, p_cold, p_elect)

    # 6. Сборка долгов и итогов
    d_209 = draft.debt_209 or Decimal("0.00") if draft else Decimal("0.00")
    o_209 = draft.overpayment_209 or Decimal("0.00") if draft else Decimal("0.00")
    d_205 = draft.debt_205 or Decimal("0.00") if draft else Decimal("0.00")
    o_205 = draft.overpayment_205 or Decimal("0.00") if draft else Decimal("0.00")

    cost_rent = costs['cost_social_rent']
    cost_utils = costs['total_cost'] - cost_rent

    total_209 = cost_utils + d_209 - o_209 + adj_map.get('209', Decimal("0.00"))
    total_205 = cost_rent + d_205 - o_205 + adj_map.get('205', Decimal("0.00"))
    grand_total = total_209 + total_205

    # 7. СОХРАНЕНИЕ
    if draft:
        if draft.is_approved:
            raise HTTPException(400, "Ваши показания уже проверены и приняты бухгалтерией. Изменение невозможно.")

        old_record = {
            "hot": str(draft.hot_water),
            "cold": str(draft.cold_water),
            "elect": str(draft.electricity),
            "date": datetime.utcnow().strftime("%d.%m.%Y %H:%M")
        }
        history_list = draft.edit_history if draft.edit_history else []
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
            room_id=user.room_id,
            period_id=period.id,
            hot_water=hot,
            cold_water=cold,
            electricity=elect,
            debt_209=Decimal("0.00"),
            overpayment_209=Decimal("0.00"),
            debt_205=Decimal("0.00"),
            overpayment_205=Decimal("0.00"),
            total_209=total_209,
            total_205=total_205,
            total_cost=grand_total,
            is_approved=False,
            anomaly_flags="PENDING",
            anomaly_score=0,
            edit_count=1,
            edit_history=[],
            **costs_for_create
        )
        db.add(new_draft)
        await db.flush()
        reading_id_for_celery = new_draft.id

    await db.commit()

    # 8. Запускаем асинхронную проверку на аномалии
    detect_anomalies_task.delay(reading_id_for_celery)

    return {"status": "success", "total_cost": grand_total, "total_209": total_209, "total_205": total_205}


# =========================
# FINANCE / DEBT (client)
# =========================
@router.get("/api/client/finance")
async def get_client_finance(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    """
    Возвращает финансовый статус текущего жильца:
    суммарные долги/переплаты по счетам 209 и 205 и текущий total_cost.

    Это облегчённая версия /api/financier/users-status (которая только для admin/financier).
    Каждый жилец видит только свои данные.
    """
    # Активный период — для current_total_cost.
    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    active_period_id = active_period.id if active_period else None

    agg_stmt = select(
        func.coalesce(func.sum(MeterReading.debt_209), 0).label("debt_209"),
        func.coalesce(func.sum(MeterReading.overpayment_209), 0).label("overpayment_209"),
        func.coalesce(func.sum(MeterReading.debt_205), 0).label("debt_205"),
        func.coalesce(func.sum(MeterReading.overpayment_205), 0).label("overpayment_205"),
    ).where(
        MeterReading.user_id == current_user.id,
        MeterReading.is_approved.is_(True),
    )

    agg_row = (await db.execute(agg_stmt)).first()
    debt_209, overpay_209, debt_205, overpay_205 = agg_row

    # Текущая ожидаемая сумма к оплате за активный период (draft или утверждённая).
    current_total = Decimal("0.00")
    if active_period_id is not None:
        current_reading = (await db.execute(
            select(MeterReading).where(
                MeterReading.user_id == current_user.id,
                MeterReading.period_id == active_period_id,
            ).order_by(MeterReading.created_at.desc()).limit(1)
        )).scalars().first()
        if current_reading and current_reading.total_cost is not None:
            current_total = Decimal(current_reading.total_cost)

    return {
        "debt_209": Decimal(debt_209 or 0),
        "overpayment_209": Decimal(overpay_209 or 0),
        "debt_205": Decimal(debt_205 or 0),
        "overpayment_205": Decimal(overpay_205 or 0),
        "total_debt": Decimal(debt_209 or 0) + Decimal(debt_205 or 0),
        "total_overpayment": Decimal(overpay_209 or 0) + Decimal(overpay_205 or 0),
        "current_period_total": current_total,
        "current_period_name": active_period.name if active_period else None,
    }


# =========================
# HISTORY
# =========================
@router.get("/api/readings/history")
async def get_client_history(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
        page: int = 1,
        limit: int = 24,
        paginated: bool = False,
):
    """
    История подачи показаний.

    Обратно-совместимо: если `paginated=false` (по-умолчанию), возвращаем
    плоский массив [...] как раньше — для старых клиентов (web, tg).

    Если `paginated=true` — возвращаем расширенный формат с метаданными:
        {
          "items": [...],
          "page": 1, "limit": 24,
          "total": N, "has_more": bool
        }
    Этот формат использует новый мобильный клиент (для InfiniteScroll).
    """
    # Защита от плохих параметров
    if page < 1:
        page = 1
    if limit < 1 or limit > 100:
        limit = 24

    offset = (page - 1) * limit
    readings = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.period))
        .where(
            MeterReading.user_id == current_user.id,
            MeterReading.is_approved.is_(True),
        )
        .order_by(MeterReading.created_at.desc())
        .offset(offset)
        .limit(limit)
    )).scalars().all()

    items = [
        {
            "id": r.id,
            "period": r.period.name if r.period else "Неизвестно",
            "hot": r.hot_water,
            "cold": r.cold_water,
            "electric": r.electricity,
            "total": r.total_cost,
            "date": r.created_at,
        }
        for r in readings
    ]

    if not paginated:
        # Старый формат — массив. Для совместимости с web/tg клиентами.
        return items

    # Новый формат — для мобильного и любых пагинированных клиентов.
    total = (await db.execute(
        select(func.count(MeterReading.id)).where(
            MeterReading.user_id == current_user.id,
            MeterReading.is_approved.is_(True),
        )
    )).scalar_one()

    return {
        "items": items,
        "page": page,
        "limit": limit,
        "total": total,
        "has_more": (offset + len(items)) < total,
    }


# =========================
# RECEIPT
# =========================

async def _prepare_client_receipt_context(
        reading_id: int,
        current_user: User,
        db: AsyncSession,
):
    """
    Подготавливает данные для генерации PDF квитанции клиента.
    Выполняет все проверки доступа и возвращает (reading, tariff, prev_reading, adjustments).
    """
    reading = (await db.execute(
        select(MeterReading)
        .options(
            selectinload(MeterReading.user),
            selectinload(MeterReading.period),
            selectinload(MeterReading.room)
        )
        .where(MeterReading.id == reading_id)
    )).scalars().first()

    if not reading:
        raise HTTPException(404, "Квитанция не найдена")

    # ВАЖНО: клиент может скачать только свою квитанцию.
    if reading.user_id != current_user.id:
        raise HTTPException(404, "Квитанция не найдена")

    if reading.room_id != current_user.room_id:
        raise HTTPException(404, "Квитанция не найдена")

    if not reading.is_approved:
        raise HTTPException(400, "Квитанция еще не сформирована")

    tariff = (await db.execute(
        select(Tariff).where(Tariff.is_active).order_by(Tariff.valid_from.desc())
    )).scalars().first()
    if not tariff:
        raise HTTPException(500, "Тариф не найден")

    prev = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.room_id == reading.room_id,
            MeterReading.is_approved.is_(True),
            MeterReading.created_at < reading.created_at
        )
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )).scalars().first()

    adjustments = (await db.execute(
        select(Adjustment).where(
            Adjustment.user_id == reading.user_id,
            Adjustment.period_id == reading.period_id
        )
    )).scalars().all()

    return reading, tariff, prev, adjustments


@router.get("/api/client/receipts/{reading_id}")
async def download_client_receipt(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Возвращает presigned S3 URL на PDF квитанции.
    Используется мобильным клиентом (он сам скачивает файл по URL).

    Для web-интерфейса используй /api/client/receipts/{id}/download,
    который стримит PDF напрямую с правильными заголовками.
    """
    reading, tariff, prev, adjustments = await _prepare_client_receipt_context(
        reading_id, current_user, db
    )

    try:
        pdf_path = await asyncio.to_thread(
            generate_receipt_pdf,
            reading=reading,
            user=reading.user,
            room=reading.room,
            period=reading.period,
            tariff=tariff,
            prev_reading=prev,
            adjustments=adjustments
        )

        s3_key = f"receipts/{reading.id}.pdf"
        upload_success = await asyncio.to_thread(s3_service.upload_file, pdf_path, s3_key)

        if not upload_success:
            # S3 недоступен — вернём стабильную ссылку на streaming-эндпоинт.
            # Мобильный клиент по ней сможет скачать PDF с Bearer-токеном.
            return {"url": f"/api/client/receipts/{reading.id}/download"}

        url = await asyncio.to_thread(s3_service.get_presigned_url, s3_key, 300)
        if not url:
            return {"url": f"/api/client/receipts/{reading.id}/download"}

        return {"url": url}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating receipt for reading_id={reading_id}: {e}", exc_info=True)
        raise HTTPException(500, "Ошибка генерации квитанции. Попробуйте позже.")


@router.get("/api/client/receipts/{reading_id}/download")
async def stream_client_receipt(
        reading_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Стримит PDF квитанции напрямую через FastAPI с заголовком Content-Disposition: attachment.
    Используется web-интерфейсом (через api.download()) — не требует S3, не зависит
    от presigned URL и не делает лишних редиректов.

    Этот эндпоинт решает баг «при клике на PDF платформа перекидывает на portal.html»,
    т.к. ответ всегда приходит напрямую в рамках текущей авторизованной сессии.
    """
    reading, tariff, prev, adjustments = await _prepare_client_receipt_context(
        reading_id, current_user, db
    )

    try:
        pdf_path = await asyncio.to_thread(
            generate_receipt_pdf,
            reading=reading,
            user=reading.user,
            room=reading.room,
            period=reading.period,
            tariff=tariff,
            prev_reading=prev,
            adjustments=adjustments,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating receipt for reading_id={reading_id}: {e}", exc_info=True)
        raise HTTPException(500, "Ошибка генерации квитанции. Попробуйте позже.")

    if not os.path.exists(pdf_path):
        raise HTTPException(500, "Не удалось получить файл квитанции на сервере")

    # Имя файла в русском формате с поддержкой UTF-8 (RFC 5987)
    period_label = (reading.period.name or "period").replace(" ", "_")
    room_label = (reading.room.room_number or "room").replace(" ", "_")
    filename = f"Kvitanciya_{room_label}_{period_label}.pdf"
    encoded_filename = quote(filename)

    return FileResponse(
        path=pdf_path,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f"attachment; filename*=utf-8''{encoded_filename}",
            # no-store, чтобы при повторной подаче квитанция пересобиралась всегда
            "Cache-Control": "no-store, must-revalidate",
        },
    )