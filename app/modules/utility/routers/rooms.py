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