# app/modules/utility/routers/admin_initial_readings.py

"""
Загрузка начальных показаний счётчиков.

ПРОБЛЕМА: При запуске проекта все счётчики начинаются с 0.
Когда жилец передаёт первые реальные показания (например ГВС=150.5),
система считает потребление как 150.5 - 0 = 150.5 м³ → огромный счёт.

РЕШЕНИЕ: Администратор загружает начальные показания (baseline) для каждой комнаты.
Они сохраняются как утверждённая запись MeterReading с флагом INITIAL_SETUP.
После этого первый расчёт жильца будет: 151.2 - 150.5 = 0.7 м³ → нормальный счёт.
"""

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.modules.utility.models import User, Room, MeterReading
from app.core.dependencies import RoleChecker

router = APIRouter(prefix="/api/rooms", tags=["Initial Readings"])
logger = logging.getLogger(__name__)

ZERO = Decimal("0.000")
allow_management = RoleChecker(["accountant", "admin"])


# =====================================================================
# УСТАНОВКА НАЧАЛЬНЫХ ПОКАЗАНИЙ ДЛЯ ОДНОЙ КОМНАТЫ
# =====================================================================
@router.post("/{room_id}/initial-readings", summary="Установить начальные показания счётчиков")
async def set_initial_readings(
    room_id: int,
    hot_water: Decimal = Query(..., ge=0, description="Текущее показание ГВС"),
    cold_water: Decimal = Query(..., ge=0, description="Текущее показание ХВС"),
    electricity: Decimal = Query(..., ge=0, description="Текущее показание электричества"),
    current_user: User = Depends(allow_management),
    db: AsyncSession = Depends(get_db)
):
    """
    Устанавливает начальные (базовые) показания счётчиков для комнаты.

    Создаёт утверждённую запись MeterReading с флагом INITIAL_SETUP.
    Обновляет кэш комнаты (last_hot_water и т.д.).

    Если начальные показания уже были установлены — перезаписывает их.
    """
    room = await db.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Комната не найдена")

    # Ищем существующую начальную запись для этой комнаты
    existing = (await db.execute(
        select(MeterReading).where(
            MeterReading.room_id == room_id,
            MeterReading.anomaly_flags == "INITIAL_SETUP"
        )
    )).scalars().first()

    if existing:
        # Обновляем существующую
        existing.hot_water = hot_water
        existing.cold_water = cold_water
        existing.electricity = electricity
        db.add(existing)
    else:
        # Создаём новую
        # Находим любого жильца комнаты для user_id (или NULL)
        user_result = await db.execute(
            select(User.id).where(
                User.room_id == room_id,
                User.is_deleted.is_(False)
            ).limit(1)
        )
        user_id = user_result.scalar_one_or_none()

        reading = MeterReading(
            source="admin",
            room_id=room_id,
            user_id=user_id,
            period_id=None,  # Начальные показания не привязаны к периоду
            hot_water=hot_water,
            cold_water=cold_water,
            electricity=electricity,
            is_approved=True,  # Сразу утверждено — это baseline
            anomaly_flags="INITIAL_SETUP",
            anomaly_score=0,
            total_209=ZERO,
            total_205=ZERO,
            # total_cost вычисляется триггером trg_readings_sync_total_cost.
        )
        db.add(reading)

    # Обновляем кэш комнаты
    room.last_hot_water = hot_water
    room.last_cold_water = cold_water
    room.last_electricity = electricity
    db.add(room)

    await db.commit()

    logger.info(
        f"Initial readings set for room {room_id}: "
        f"hot={hot_water}, cold={cold_water}, elect={electricity} "
        f"by {current_user.username}"
    )

    return {
        "status": "success",
        "room_id": room_id,
        "hot_water": str(hot_water),
        "cold_water": str(cold_water),
        "electricity": str(electricity),
    }


# =====================================================================
# ПОЛУЧЕНИЕ ТЕКУЩИХ БАЗОВЫХ ПОКАЗАНИЙ КОМНАТЫ
# =====================================================================
@router.get("/{room_id}/current-readings", summary="Текущие показания счётчиков комнаты")
async def get_current_readings(
    room_id: int,
    current_user: User = Depends(allow_management),
    db: AsyncSession = Depends(get_db)
):
    """Возвращает последние известные показания комнаты (из кэша или из истории)."""
    room = await db.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Комната не найдена")

    return {
        "room_id": room_id,
        "dormitory_name": room.dormitory_name,
        "room_number": room.room_number,
        "hot_water": str(room.last_hot_water or ZERO),
        "cold_water": str(room.last_cold_water or ZERO),
        "electricity": str(room.last_electricity or ZERO),
    }
