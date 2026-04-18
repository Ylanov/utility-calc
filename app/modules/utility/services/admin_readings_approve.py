# app/modules/utility/services/admin_readings_approve.py

import asyncio
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, update, desc
from sqlalchemy.orm import selectinload

from app.modules.utility.models import User, MeterReading, Tariff, BillingPeriod, Adjustment, Room
from app.modules.utility.schemas import ApproveRequest
from app.modules.utility.services.calculations import calculate_utilities, D
from app.modules.utility.services.notification_service import send_push_to_user

# ИМПОРТ ДЛЯ ЖУРНАЛА ДЕЙСТВИЙ
from app.modules.utility.routers.admin_dashboard import write_audit_log

ZERO = Decimal("0.00")


async def bulk_approve_drafts(db: AsyncSession, current_user=None):
    """
    Массовое утверждение всех безопасных черновиков.
    Используется пакетное обновление (bulk update) для предотвращения
    долгих блокировок БД (deadlocks) и экономии памяти.
    """
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))).scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода")

    active_tariffs = (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().all()
    if not active_tariffs:
        raise HTTPException(500, detail="Активные тарифы не найдены")

    tariffs_map = {t.id: t for t in active_tariffs}
    default_tariff = tariffs_map.get(1) or active_tariffs[0]

    # Убрали .with_for_update().
    # Это позволяет базе "дышать" и принимать другие запросы во время расчетов.
    # Если кто-то параллельно нажмет "Утвердить", операции будут идемпотентны.
    drafts_rows = (await db.execute(
        select(MeterReading, User, Room)
        .join(User, MeterReading.user_id == User.id)
        .join(Room, MeterReading.room_id == Room.id)
        .where(
            MeterReading.is_approved.is_(False),
            MeterReading.period_id == active_period.id,
            MeterReading.anomaly_score < 80
        )
    )).all()

    if not drafts_rows:
        return {"status": "success", "approved_count": 0}

    room_ids = [row[2].id for row in drafts_rows]
    user_ids = [row[1].id for row in drafts_rows]

    # Загружаем предыдущие показания для расчета дельты
    subq_max_date = select(
        MeterReading.room_id,
        func.max(MeterReading.created_at).label("max_created")
    ).where(
        MeterReading.room_id.in_(room_ids),
        MeterReading.is_approved.is_(True)
    ).group_by(MeterReading.room_id).subquery()

    prev_readings_map = {r.room_id: r for r in (await db.execute(
        select(MeterReading).join(
            subq_max_date,
            (MeterReading.room_id == subq_max_date.c.room_id) &
            (MeterReading.created_at == subq_max_date.c.max_created)
        )
    )).scalars().all()}

    # Загружаем корректировки (долги / скидки)
    adj_res = await db.execute(
        select(
            Adjustment.user_id,
            Adjustment.account_type,
            func.sum(Adjustment.amount).label("total")
        ).where(
            Adjustment.period_id == active_period.id,
            Adjustment.user_id.in_(user_ids)
        ).group_by(Adjustment.user_id, Adjustment.account_type)
    )

    adj_map = {}
    for uid, acc_type, amount in adj_res.all():
        if uid not in adj_map:
            adj_map[uid] = {'209': ZERO, '205': ZERO}
        adj_map[uid][str(acc_type)] = amount or ZERO

    update_mappings = []
    room_updates = []

    # Расчет в памяти
    for reading, user, room in drafts_rows:
        prev = prev_readings_map.get(room.id)
        p_hot = D(prev.hot_water) if prev else ZERO
        p_cold = D(prev.cold_water) if prev else ZERO
        p_elect = D(prev.electricity) if prev else ZERO

        cur_hot = D(reading.hot_water)
        cur_cold = D(reading.cold_water)
        cur_elect = D(reading.electricity)

        user_tariff = tariffs_map.get(user.tariff_id) if getattr(user, 'tariff_id', None) else default_tariff
        residents_count = user.residents_count if user.residents_count is not None else 1
        total_room = room.total_room_residents if room.total_room_residents > 0 else 1

        vol_hot = max(ZERO, cur_hot - p_hot)
        vol_cold = max(ZERO, cur_cold - p_cold)
        user_elect_share = max(ZERO, (Decimal(residents_count) / Decimal(total_room)) * (cur_elect - p_elect))

        costs = calculate_utilities(
            user=user,
            room=room,
            tariff=user_tariff or default_tariff,
            volume_hot=vol_hot,
            volume_cold=vol_cold,
            volume_sewage=vol_hot + vol_cold,
            volume_electricity_share=user_elect_share
        )

        user_adjs = adj_map.get(user.id, {'209': ZERO, '205': ZERO})
        cost_rent_205 = costs['cost_social_rent']
        cost_utils_209 = costs['total_cost'] - cost_rent_205

        total_209 = cost_utils_209 + (reading.debt_209 or ZERO) - (reading.overpayment_209 or ZERO) + user_adjs['209']
        total_205 = cost_rent_205 + (reading.debt_205 or ZERO) - (reading.overpayment_205 or ZERO) + user_adjs['205']

        # Подготавливаем словари для bulk_update
        update_mappings.append({
            "id": reading.id,
            "is_approved": True,
            "total_cost": total_209 + total_205,
            "total_209": total_209,
            "total_205": total_205,
            **costs
        })

        room_updates.append({
            "id": room.id,
            "last_hot_water": reading.hot_water,
            "last_cold_water": reading.cold_water,
            "last_electricity": reading.electricity
        })

    # Пакетное обновление (Chunking). Сохраняет RAM и ускоряет работу БД в разы.
    if update_mappings:
        chunk_size = 1000
        for i in range(0, len(update_mappings), chunk_size):
            await db.execute(update(MeterReading), update_mappings[i:i + chunk_size])
            await db.execute(update(Room), room_updates[i:i + chunk_size])

        # ЗАПИСЬ В ЖУРНАЛ: Массовое утверждение
        uid = current_user.id if current_user else None
        uname = current_user.username if current_user else "Система"
        await write_audit_log(
            db, user_id=uid, username=uname,
            action="approve_bulk", entity_type="reading",
            details={"approved_count": len(update_mappings)}
        )

        await db.commit()

    return {"status": "success", "approved_count": len(update_mappings)}


async def approve_single(db: AsyncSession, reading_id: int, correction_data: ApproveRequest, current_user=None):
    """Ручное утверждение бухгалтером с возможными корректировками объема.

    ИСПРАВЛЕНИЕ: при одновременном approve двумя админами одного reading_id
    раньше не было блокировки — оба успевали пройти проверку `is_approved=False`
    и сделать commit. В итоге финансовая сумма записывалась дважды, пуш-уведомление
    уходило дважды, а в журнал шли два события approve.

    SELECT ... FOR UPDATE заставляет второго админа ждать первого;
    когда он получит контроль, проверка `if reading.is_approved` уже сработает
    и второе утверждение будет отклонено с 409.
    """
    reading = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.user).selectinload(User.room))
        .where(MeterReading.id == reading_id)
        .with_for_update()
    )).scalars().first()

    if not reading:
        raise HTTPException(status_code=404, detail="Показания не найдены")
    if reading.is_approved:
        # 409 Conflict точнее отражает «уже утверждено другим админом».
        raise HTTPException(status_code=409, detail="Показание уже утверждено другим администратором")

    user = reading.user
    room = user.room
    if not room:
        raise HTTPException(status_code=400, detail="Жилец не привязан к помещению")

    t = (await db.execute(
        select(Tariff).where(Tariff.id == (getattr(user, 'tariff_id', None) or 1)))
         ).scalars().first() or (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()

    prev = (await db.execute(
        select(MeterReading)
        .where(MeterReading.room_id == room.id, MeterReading.is_approved)
        .order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    p_hot = D(prev.hot_water) if prev else ZERO
    p_cold = D(prev.cold_water) if prev else ZERO
    p_elect = D(prev.electricity) if prev else ZERO

    d_hot_final = max(ZERO, (D(reading.hot_water) - p_hot) - correction_data.hot_correction)
    d_cold_final = max(ZERO, (D(reading.cold_water) - p_cold) - correction_data.cold_correction)

    residents_count = user.residents_count if user.residents_count is not None else 1
    total_room = room.total_room_residents if room.total_room_residents > 0 else 1

    d_elect_final = max(ZERO, ((Decimal(residents_count) / Decimal(total_room)) * (
            D(reading.electricity) - p_elect)) - correction_data.electricity_correction)

    costs = calculate_utilities(
        user=user,
        room=room,
        tariff=t,
        volume_hot=d_hot_final,
        volume_cold=d_cold_final,
        volume_sewage=max(ZERO, (d_hot_final + d_cold_final) - correction_data.sewage_correction),
        volume_electricity_share=d_elect_final
    )

    adj_res = await db.execute(
        select(Adjustment.account_type, func.sum(Adjustment.amount))
        .where(Adjustment.user_id == user.id, Adjustment.period_id == reading.period_id)
        .group_by(Adjustment.account_type)
    )
    adj_map = {row[0]: (row[1] or ZERO) for row in adj_res.all()}

    cost_rent_205 = costs['cost_social_rent']
    total_209 = (costs['total_cost'] - cost_rent_205) + (reading.debt_209 or ZERO) - (
            reading.overpayment_209 or ZERO) + adj_map.get('209', ZERO)

    total_205 = cost_rent_205 + (reading.debt_205 or ZERO) - (reading.overpayment_205 or ZERO) + adj_map.get('205',
                                                                                                             ZERO)

    reading.hot_correction = correction_data.hot_correction
    reading.cold_correction = correction_data.cold_correction
    reading.electricity_correction = correction_data.electricity_correction
    reading.sewage_correction = correction_data.sewage_correction

    reading.total_209 = total_209
    reading.total_205 = total_205
    reading.total_cost = total_209 + total_205
    reading.is_approved = True

    for k, v in costs.items():
        if hasattr(reading, k):
            setattr(reading, k, v)

    room.last_hot_water = reading.hot_water
    room.last_cold_water = reading.cold_water
    room.last_electricity = reading.electricity

    db.add(room)

    # ЗАПИСЬ В ЖУРНАЛ: Ручное утверждение бухгалтером
    uid = current_user.id if current_user else None
    uname = current_user.username if current_user else "Бухгалтер"
    await write_audit_log(
        db, user_id=uid, username=uname,
        action="approve", entity_type="reading", entity_id=reading.id,
        details={"total_sum": str(total_209 + total_205), "owner": user.username}
    )

    await db.commit()

    # ---> ОТПРАВЛЯЕМ ПУШ КОНКРЕТНОМУ ЖИЛЬЦУ <---
    # Используем asyncio.create_task для фоновой отправки без блокировки ответа
    final_sum = total_209 + total_205
    asyncio.create_task(
        send_push_to_user(
            db,
            user_id=user.id,
            title="✅ Квитанция проверена",
            body=f"Бухгалтерия утвердила ваши показания. Итого к оплате: {final_sum} руб."
        )
    )

    return {"status": "approved", "new_total": final_sum}