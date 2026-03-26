# app/modules/utility/routers/rooms.py
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, or_, desc, asc
from typing import Optional, List

from app.core.database import get_db
from app.modules.utility.models import Room, User, MeterReading
from app.modules.utility.schemas import RoomCreate, RoomUpdate, RoomResponse, PaginatedResponse
from app.core.dependencies import get_current_user, RoleChecker

router = APIRouter(prefix="/api/rooms", tags=["Housing"])
allow_management = RoleChecker(["accountant", "admin", "financier"])


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