# app/modules/utility/routers/rooms.py
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, or_, desc, asc
from typing import Optional, List

from app.core.database import get_db
from app.modules.utility.models import Room, User, MeterReading, BillingPeriod, Tariff
from app.modules.utility.schemas import RoomCreate, RoomUpdate, RoomResponse, PaginatedResponse, ReplaceMeterSchema
from app.core.dependencies import get_current_user, RoleChecker
from app.modules.utility.services.calculations import calculate_utilities

router = APIRouter(prefix="/api/rooms", tags=["Housing"])
allow_management = RoleChecker(["accountant", "admin", "financier"])

ZERO = Decimal("0.00")


@router.get("/analyze", summary="Мощный анализатор Жилфонда", dependencies=[Depends(allow_management)])
async def analyze_housing(db: AsyncSession = Depends(get_db)):
    """
    Сканирует весь жилфонд и пользователей на наличие аномалий:
    - Раздельные лицевые счета в одной комнате (Холостяки)
    - Перенаселение (платят за большее кол-во человек, чем вмещает комната)
    - Недобор / Пустые места
    - Жильцы без комнаты (ошибки привязки)
    - Комнаты с нулевой площадью
    """
    # 1. Получаем все комнаты
    rooms_res = await db.execute(select(Room))
    rooms = rooms_res.scalars().all()

    # 2. Получаем всех активных жильцов
    users_res = await db.execute(select(User).where(User.is_deleted.is_(False)))
    users = users_res.scalars().all()

    # 3. Группируем жильцов по комнатам
    room_users = {}
    unattached_users = []

    for u in users:
        if not u.room_id:
            unattached_users.append(u)
        else:
            if u.room_id not in room_users:
                room_users[u.room_id] = []
            room_users[u.room_id].append(u)

    issues = {
        "shared_billing": [],  # Раздельные счета (холостяки)
        "overcrowded": [],  # Платят за больше человек, чем мест
        "underpopulated": [],  # Платят за меньше человек, чем мест (пустующие места)
        "zero_area": [],  # Нулевая площадь
        "empty_rooms": [],  # Пустые комнаты
        "unattached_users": []  # Жильцы без комнаты
    }

    for room in rooms:
        r_name = f"{room.dormitory_name}, ком. {room.room_number}"
        r_id = room.id

        # Ошибка: нулевая площадь
        if room.apartment_area <= 0:
            issues["zero_area"].append({"id": r_id, "title": r_name,
                                        "desc": "Указана нулевая площадь. Коммуналка по нормативу не будет начислена корректно."})

        occupants = room_users.get(room.id, [])

        # Инфо: Пустая комната
        if not occupants:
            issues["empty_rooms"].append({"id": r_id, "title": r_name, "desc": "Никто не прописан (нет активных Л/С)"})
            continue

        acc_count = len(occupants)
        paying_res = sum((u.residents_count or 1) for u in occupants)
        usernames = ", ".join([u.username for u in occupants])

        # Аномалия 1: Холостяки / Раздельные счета (2 и более Л/С в одной комнате)
        if acc_count > 1:
            issues["shared_billing"].append({
                "id": r_id, "title": r_name,
                "desc": f"Раздельные счета ({acc_count} Л/С) в одном помещении: {usernames}. Убедитесь, что это не дубликаты."
            })

        # Аномалия 2: Перенаселение
        if paying_res > room.total_room_residents:
            issues["overcrowded"].append({
                "id": r_id, "title": r_name,
                "desc": f"Платят суммарно за {paying_res} чел., хотя максимальная вместимость комнаты {room.total_room_residents} чел. ({usernames})"
            })

        # Аномалия 3: Недобор
        elif paying_res < room.total_room_residents:
            issues["underpopulated"].append({
                "id": r_id, "title": r_name,
                "desc": f"Платят за {paying_res} чел., а макс. мест {room.total_room_residents}. Либо кто-то не платит, либо в комнате есть свободные места."
            })

    # Аномалия 4: Жильцы-призраки
    for u in unattached_users:
        issues["unattached_users"].append({"id": u.id, "title": u.username,
                                           "desc": "Не привязан ни к одной комнате (Начисления по счетчикам невозможны)"})

    return issues


@router.get("/dormitories", response_model=List[str], dependencies=[Depends(get_current_user)])
async def get_dormitories(db: AsyncSession = Depends(get_db)):
    """Получает список всех уникальных названий общежитий."""
    result = await db.execute(select(Room.dormitory_name).distinct().order_by(Room.dormitory_name))
    return result.scalars().all()


@router.get("", response_model=PaginatedResponse[RoomResponse], dependencies=[Depends(allow_management)])
async def get_rooms(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=1000),
        search: Optional[str] = Query(None),
        dormitory: Optional[str] = Query(None),
        db: AsyncSession = Depends(get_db)
):
    """Список комнат с пагинацией и поиском."""
    query = select(Room)
    count_query = select(func.count(Room.id))

    if search:
        search_filter = f"%{search}%"
        condition = or_(
            Room.room_number.ilike(search_filter),
            Room.hw_meter_serial.ilike(search_filter),
            Room.cw_meter_serial.ilike(search_filter)
        )
        query = query.where(condition)
        count_query = count_query.where(condition)

    if dormitory:
        query = query.where(Room.dormitory_name == dormitory)
        count_query = count_query.where(Room.dormitory_name == dormitory)

    total = (await db.execute(count_query)).scalar_one()

    query = query.order_by(Room.dormitory_name, Room.room_number).offset((page - 1) * limit).limit(limit)
    items = (await db.execute(query)).scalars().all()

    return {"total": total, "page": page, "size": limit, "items": items}


@router.post("", response_model=RoomResponse, dependencies=[Depends(allow_management)])
async def create_room(data: RoomCreate, db: AsyncSession = Depends(get_db)):
    """Создает новую комнату."""
    # Проверка на дубликат
    exist = await db.execute(select(Room).where(
        Room.dormitory_name == data.dormitory_name,
        Room.room_number == data.room_number
    ))
    if exist.scalars().first():
        raise HTTPException(status_code=400, detail="Такая комната в этом общежитии уже существует")

    room = Room(**data.dict())
    db.add(room)
    await db.commit()
    await db.refresh(room)
    return room


@router.get("/{room_id}", response_model=RoomResponse, dependencies=[Depends(allow_management)])
async def get_room(room_id: int, db: AsyncSession = Depends(get_db)):
    room = await db.get(Room, room_id)
    if not room: raise HTTPException(status_code=404, detail="Комната не найдена")
    return room


@router.put("/{room_id}", response_model=RoomResponse, dependencies=[Depends(allow_management)])
async def update_room(room_id: int, data: RoomUpdate, db: AsyncSession = Depends(get_db)):
    room = await db.get(Room, room_id)
    if not room: raise HTTPException(status_code=404, detail="Комната не найдена")

    update_data = data.dict(exclude_unset=True)

    # Защита от дублей при переименовании
    if "dormitory_name" in update_data or "room_number" in update_data:
        new_dorm = update_data.get("dormitory_name", room.dormitory_name)
        new_num = update_data.get("room_number", room.room_number)
        if new_dorm != room.dormitory_name or new_num != room.room_number:
            exist = await db.execute(select(Room).where(Room.dormitory_name == new_dorm, Room.room_number == new_num))
            if exist.scalars().first():
                raise HTTPException(status_code=400, detail="Комната с таким номером уже есть в этом общежитии")

    for key, value in update_data.items():
        setattr(room, key, value)

    await db.commit()
    await db.refresh(room)
    return room


@router.delete("/{room_id}", status_code=204, dependencies=[Depends(allow_management)])
async def delete_room(room_id: int, db: AsyncSession = Depends(get_db)):
    """Удаляет комнату, если к ней не привязаны жильцы или история показаний."""
    room = await db.get(Room, room_id)
    if not room: raise HTTPException(status_code=404, detail="Комната не найдена")

    # Проверка: есть ли жильцы (даже удаленные, так как история важна)
    users_exist = await db.execute(select(User.id).where(User.room_id == room_id).limit(1))
    if users_exist.scalars().first():
        raise HTTPException(status_code=400, detail="Нельзя удалить комнату, к которой привязаны жильцы (даже бывшие)")

    # Проверка: есть ли показания счетчиков
    readings_exist = await db.execute(select(MeterReading.id).where(MeterReading.room_id == room_id).limit(1))
    if readings_exist.scalars().first():
        raise HTTPException(status_code=400, detail="Нельзя удалить комнату, по которой есть история показаний")

    await db.delete(room)
    await db.commit()
    return None


@router.post("/{room_id}/replace-meter", dependencies=[Depends(allow_management)])
async def replace_meter(room_id: int, data: ReplaceMeterSchema, db: AsyncSession = Depends(get_db)):
    """
    Безопасная замена счетчика.
    Рассчитывает долг по старому счетчику и устанавливает нулевую базу для нового.
    """
    room = await db.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="Комната не найдена")

    # Ищем активный период
    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))).scalars().first()
    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного расчетного периода")

    # Ищем черновики. Если есть черновик, просим сначала его утвердить или удалить
    draft = (await db.execute(select(MeterReading).where(
        MeterReading.room_id == room.id,
        MeterReading.period_id == active_period.id,
        MeterReading.is_approved == False
    ))).scalars().first()
    if draft:
        raise HTTPException(status_code=400,
                            detail="По этой комнате висит необработанный черновик показаний. Утвердите или удалите его перед заменой счетчика.")

    # Ищем жильца для расчета (чтобы взять тариф и долю)
    user = (await db.execute(
        select(User).where(User.room_id == room.id, User.is_deleted == False).limit(1))).scalars().first()

    # Ищем последние показания
    prev_reading = (await db.execute(
        select(MeterReading).where(MeterReading.room_id == room.id, MeterReading.is_approved == True)
        .order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    p_hot = prev_reading.hot_water if prev_reading else ZERO
    p_cold = prev_reading.cold_water if prev_reading else ZERO
    p_elect = prev_reading.electricity if prev_reading else ZERO

    # 1. Расчет дельты по закрываемому счетчику
    d_hot, d_cold, d_elect = ZERO, ZERO, ZERO

    if data.meter_type == "hot":
        if data.final_old_value < p_hot: raise HTTPException(400, "Финальное показание меньше предыдущего!")
        d_hot = data.final_old_value - p_hot
        room.hw_meter_serial = data.new_serial
        c_hot, c_cold, c_elect = data.final_old_value, p_cold, p_elect
        n_hot, n_cold, n_elect = data.initial_new_value, p_cold, p_elect
    elif data.meter_type == "cold":
        if data.final_old_value < p_cold: raise HTTPException(400, "Финальное показание меньше предыдущего!")
        d_cold = data.final_old_value - p_cold
        room.cw_meter_serial = data.new_serial
        c_hot, c_cold, c_elect = p_hot, data.final_old_value, p_elect
        n_hot, n_cold, n_elect = p_hot, data.initial_new_value, p_elect
    else:  # elect
        if data.final_old_value < p_elect: raise HTTPException(400, "Финальное показание меньше предыдущего!")
        d_elect = data.final_old_value - p_elect
        room.el_meter_serial = data.new_serial
        c_hot, c_cold, c_elect = p_hot, p_cold, data.final_old_value
        n_hot, n_cold, n_elect = p_hot, p_cold, data.initial_new_value

    # Если в комнате есть жилец, начисляем ему стоимость остатка старого счетчика
    if user and (d_hot > 0 or d_cold > 0 or d_elect > 0):
        t = (await db.execute(select(Tariff).where(Tariff.id == getattr(user, 'tariff_id', 1)))).scalars().first()

        residents = Decimal(user.residents_count)
        total_res = Decimal(room.total_room_residents) if room.total_room_residents > 0 else Decimal(1)
        elect_share = (residents / total_res) * d_elect

        costs = calculate_utilities(user, room, t, d_hot, d_cold, d_hot + d_cold, elect_share)

        # Запись 1: Закрытие старого счетчика (с начислением суммы).
        #
        # ВАЖНО: переносим непогашенный долг (debt_209/205) и переплаты
        # (overpayment_209/205) из предыдущего утверждённого показания.
        # Раньше они обнулялись — жилец видел, что "долг исчез", но в БД
        # он висел на закрытом показании и при следующем расчёте не учитывался.
        closing_reading = MeterReading(
            user_id=user.id, room_id=room.id, period_id=active_period.id,
            hot_water=c_hot, cold_water=c_cold, electricity=c_elect,
            is_approved=True, anomaly_flags="METER_CLOSED", anomaly_score=0,
            total_209=costs['total_cost'] - costs['cost_social_rent'],
            total_205=costs['cost_social_rent'], total_cost=costs['total_cost'],
            debt_209=(prev_reading.debt_209 if prev_reading else ZERO) or ZERO,
            overpayment_209=(prev_reading.overpayment_209 if prev_reading else ZERO) or ZERO,
            debt_205=(prev_reading.debt_205 if prev_reading else ZERO) or ZERO,
            overpayment_205=(prev_reading.overpayment_205 if prev_reading else ZERO) or ZERO,
        )
        for k, v in costs.items(): setattr(closing_reading, k, v)
        db.add(closing_reading)
        await db.flush()

    # Запись 2: Базовый старт нового счетчика (сумма 0 руб)
    # Это нужно чтобы следующий расчет считал от initial_new_value
    base_reading = MeterReading(
        user_id=user.id if user else None, room_id=room.id, period_id=active_period.id,
        hot_water=n_hot, cold_water=n_cold, electricity=n_elect,
        is_approved=True, anomaly_flags="METER_REPLACEMENT", anomaly_score=0,
        total_209=ZERO, total_205=ZERO, total_cost=ZERO
    )
    db.add(base_reading)

    # Обновляем кэш комнаты
    room.last_hot_water = n_hot
    room.last_cold_water = n_cold
    room.last_electricity = n_elect
    db.add(room)

    await db.commit()
    return {"status": "success"}