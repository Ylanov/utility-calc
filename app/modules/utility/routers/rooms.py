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
        # Новые фильтры — без них невозможно сделать быстрый drill-down
        # по состоянию комнаты. У админа в проекте на 2000+ комнат без
        # фильтров работать нельзя.
        occupancy: Optional[str] = Query(
            None, pattern="^(empty|partial|full|overcrowded)$",
            description="empty=0 жильцов, partial<вместимости, full=, overcrowded>",
        ),
        missing_meter: Optional[bool] = Query(
            None, description="Отсутствует хотя бы один из серийников счётчиков",
        ),
        db: AsyncSession = Depends(get_db)
):
    """Список комнат с пагинацией и расширенными фильтрами."""
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

    # Фильтр по заполненности: коррелируем через subquery количества жильцов
    if occupancy:
        residents_subq = (
            select(func.count(User.id))
            .where(User.room_id == Room.id, User.is_deleted.is_(False), User.role == "user")
            .correlate(Room)
            .scalar_subquery()
        )
        cap = func.coalesce(Room.total_room_residents, 1)
        if occupancy == "empty":
            cond = residents_subq == 0
        elif occupancy == "partial":
            cond = (residents_subq > 0) & (residents_subq < cap)
        elif occupancy == "full":
            cond = residents_subq == cap
        else:  # overcrowded
            cond = residents_subq > cap
        query = query.where(cond)
        count_query = count_query.where(cond)

    if missing_meter is True:
        cond = (
            (Room.hw_meter_serial.is_(None)) | (Room.hw_meter_serial == "")
            | (Room.cw_meter_serial.is_(None)) | (Room.cw_meter_serial == "")
            | (Room.el_meter_serial.is_(None)) | (Room.el_meter_serial == "")
        )
        query = query.where(cond)
        count_query = count_query.where(cond)

    total = (await db.execute(count_query)).scalar_one()

    query = query.order_by(Room.dormitory_name, Room.room_number).offset((page - 1) * limit).limit(limit)
    items = (await db.execute(query)).scalars().all()

    return {"total": total, "page": page, "size": limit, "items": items}


# =====================================================================
# STATS — сводка по Жилфонду
# =====================================================================
@router.get("/stats", dependencies=[Depends(allow_management)])
async def housing_stats(
    dormitory: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """KPI для шапки вкладки «Жилфонд».

    Возвращает:
      * total_rooms / empty / partial / full / overcrowded
      * total_area / avg_area
      * total_capacity (суммарные места) / total_residents (сколько жильцов)
      * missing_meters_count (комнаты с неполным набором счётчиков)
      * by_dormitory (разбивка)

    Если передан `dormitory` — все метрики считаются в рамках этого общежития.
    """
    base = select(Room)
    if dormitory:
        base = base.where(Room.dormitory_name == dormitory)

    # Все комнаты с кол-вом проживающих одним запросом
    residents_subq = (
        select(
            User.room_id.label("rid"),
            func.count(User.id).label("residents"),
        )
        .where(User.is_deleted.is_(False), User.role == "user", User.room_id.is_not(None))
        .group_by(User.room_id)
        .subquery()
    )
    rows_q = (
        select(
            Room.id,
            Room.dormitory_name,
            Room.apartment_area,
            Room.total_room_residents,
            Room.hw_meter_serial,
            Room.cw_meter_serial,
            Room.el_meter_serial,
            func.coalesce(residents_subq.c.residents, 0).label("residents"),
        )
        .outerjoin(residents_subq, residents_subq.c.rid == Room.id)
    )
    if dormitory:
        rows_q = rows_q.where(Room.dormitory_name == dormitory)

    rows = (await db.execute(rows_q)).all()

    total = len(rows)
    empty = partial = full = overcrowded = 0
    total_area = Decimal("0")
    total_capacity = 0
    total_residents = 0
    missing_meters = 0
    by_dorm: dict = {}

    for rid, dorm, area, capacity, hw, cw, el, residents in rows:
        cap = int(capacity or 1)
        r = int(residents or 0)
        if r == 0:
            empty += 1
        elif r < cap:
            partial += 1
        elif r == cap:
            full += 1
        else:
            overcrowded += 1

        a = Decimal(str(area or 0))
        total_area += a
        total_capacity += cap
        total_residents += r

        if not hw or not cw or not el:
            missing_meters += 1

        d = by_dorm.setdefault(dorm or "—", {
            "rooms": 0, "residents": 0, "capacity": 0, "area": Decimal("0"),
        })
        d["rooms"] += 1
        d["residents"] += r
        d["capacity"] += cap
        d["area"] += a

    avg_area = float(total_area / total) if total else 0.0
    occupancy_pct = (
        round(total_residents / total_capacity * 100) if total_capacity else 0
    )

    return {
        "total_rooms": total,
        "empty": empty,
        "partial": partial,
        "full": full,
        "overcrowded": overcrowded,
        "total_area": float(total_area),
        "avg_area": round(avg_area, 2),
        "total_capacity": total_capacity,
        "total_residents": total_residents,
        "occupancy_pct": occupancy_pct,
        "free_slots": max(0, total_capacity - total_residents),
        "missing_meters_count": missing_meters,
        "by_dormitory": [
            {
                "name": name, "rooms": d["rooms"], "residents": d["residents"],
                "capacity": d["capacity"], "area": float(d["area"]),
                "occupancy_pct": (
                    round(d["residents"] / d["capacity"] * 100) if d["capacity"] else 0
                ),
            }
            for name, d in sorted(by_dorm.items())
        ],
    }


# =====================================================================
# EXPORT — Excel со списком комнат (с текущими фильтрами)
# =====================================================================
@router.get("/export", dependencies=[Depends(allow_management)])
async def export_rooms(
    search: Optional[str] = Query(None),
    dormitory: Optional[str] = Query(None),
    occupancy: Optional[str] = Query(None, pattern="^(empty|partial|full|overcrowded)$"),
    missing_meter: Optional[bool] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Excel-выгрузка всех отфильтрованных комнат + количество жильцов."""
    import io
    from fastapi.responses import StreamingResponse
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    # Собираем тот же запрос что и для списка — но без пагинации
    residents_subq = (
        select(
            User.room_id.label("rid"),
            func.count(User.id).label("residents"),
        )
        .where(User.is_deleted.is_(False), User.role == "user", User.room_id.is_not(None))
        .group_by(User.room_id)
        .subquery()
    )
    q = (
        select(
            Room,
            func.coalesce(residents_subq.c.residents, 0).label("residents"),
            Tariff.name.label("tariff_name"),
        )
        .outerjoin(residents_subq, residents_subq.c.rid == Room.id)
        .outerjoin(Tariff, Tariff.id == Room.tariff_id)
    )
    if search:
        pat = f"%{search}%"
        q = q.where(or_(
            Room.room_number.ilike(pat),
            Room.hw_meter_serial.ilike(pat),
            Room.cw_meter_serial.ilike(pat),
        ))
    if dormitory:
        q = q.where(Room.dormitory_name == dormitory)
    if occupancy:
        cap = func.coalesce(Room.total_room_residents, 1)
        if occupancy == "empty":
            q = q.where(residents_subq.c.residents.is_(None) | (residents_subq.c.residents == 0))
        elif occupancy == "partial":
            q = q.where((residents_subq.c.residents > 0) & (residents_subq.c.residents < cap))
        elif occupancy == "full":
            q = q.where(residents_subq.c.residents == cap)
        elif occupancy == "overcrowded":
            q = q.where(residents_subq.c.residents > cap)
    if missing_meter is True:
        q = q.where(
            (Room.hw_meter_serial.is_(None)) | (Room.hw_meter_serial == "")
            | (Room.cw_meter_serial.is_(None)) | (Room.cw_meter_serial == "")
            | (Room.el_meter_serial.is_(None)) | (Room.el_meter_serial == "")
        )
    q = q.order_by(Room.dormitory_name, Room.room_number)

    rows = (await db.execute(q)).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Жилфонд"
    headers = [
        "ID", "Общежитие", "Комната", "Площадь м²", "Мест", "Жильцов",
        "Заполненность", "Тариф", "№ ГВС", "№ ХВС", "№ Электр.",
    ]
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DBEAFE")
    for i, (room, residents, tariff_name) in enumerate(rows, 2):
        cap = int(room.total_room_residents or 1)
        r = int(residents or 0)
        status = (
            "Переполнена" if r > cap
            else "Полная" if r == cap
            else "Частичная" if r > 0
            else "Пустая"
        )
        ws.cell(row=i, column=1, value=room.id)
        ws.cell(row=i, column=2, value=room.dormitory_name)
        ws.cell(row=i, column=3, value=room.room_number)
        ws.cell(row=i, column=4, value=float(room.apartment_area or 0))
        ws.cell(row=i, column=5, value=cap)
        ws.cell(row=i, column=6, value=r)
        ws.cell(row=i, column=7, value=status)
        ws.cell(row=i, column=8, value=tariff_name or "")
        ws.cell(row=i, column=9, value=room.hw_meter_serial or "")
        ws.cell(row=i, column=10, value=room.cw_meter_serial or "")
        ws.cell(row=i, column=11, value=room.el_meter_serial or "")

    for col, w in [("A", 6), ("B", 28), ("C", 10), ("D", 10), ("E", 6),
                   ("F", 8), ("G", 14), ("H", 22), ("I", 14), ("J", 14), ("K", 14)]:
        ws.column_dimensions[col].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from datetime import datetime as _dt
    fname = f"housing_{_dt.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


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


@router.get("/{room_id}/residents", dependencies=[Depends(allow_management)])
async def room_residents(
    room_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Список жильцов комнаты + последнее утверждённое показание.
    Используется раскрывающимся блоком в таблице Жилфонда."""
    from sqlalchemy.orm import selectinload

    room = await db.get(Room, room_id)
    if not room:
        raise HTTPException(404, "Комната не найдена")

    users = (await db.execute(
        select(User)
        .options(selectinload(User.tariff))
        .where(User.room_id == room_id, User.is_deleted.is_(False))
        .order_by(User.username)
    )).scalars().all()

    # Последнее утверждённое показание в комнате (общее для всех жильцов)
    last_reading = (await db.execute(
        select(MeterReading)
        .where(MeterReading.room_id == room_id, MeterReading.is_approved.is_(True))
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )).scalars().first()

    return {
        "room": {
            "id": room.id,
            "dormitory_name": room.dormitory_name,
            "room_number": room.room_number,
            "apartment_area": float(room.apartment_area or 0),
            "capacity": int(room.total_room_residents or 1),
            "tariff_id": room.tariff_id,
        },
        "residents": [
            {
                "id": u.id, "username": u.username,
                "resident_type": u.resident_type, "billing_mode": u.billing_mode,
                "residents_count": u.residents_count,
                "tariff_id": u.tariff_id,
                "tariff_name": u.tariff.name if u.tariff else None,
                "workplace": u.workplace,
            }
            for u in users
        ],
        "last_reading": {
            "created_at": last_reading.created_at.isoformat() if last_reading and last_reading.created_at else None,
            "hot_water": float(last_reading.hot_water) if last_reading else None,
            "cold_water": float(last_reading.cold_water) if last_reading else None,
            "electricity": float(last_reading.electricity) if last_reading else None,
        } if last_reading else None,
    }


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

    # Если меняется tariff_id комнаты — валидируем и инвалидируем кеш расчётов
    tariff_changed = "tariff_id" in update_data and update_data["tariff_id"] != room.tariff_id
    if tariff_changed and update_data["tariff_id"] is not None:
        t = await db.get(Tariff, update_data["tariff_id"])
        if not t or not t.is_active:
            raise HTTPException(400, "Активный тариф с таким id не найден")

    for key, value in update_data.items():
        setattr(room, key, value)

    await db.commit()
    await db.refresh(room)

    if tariff_changed:
        from app.modules.utility.services.tariff_cache import tariff_cache
        tariff_cache.invalidate()
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
        from app.modules.utility.services.tariff_cache import tariff_cache
        t = tariff_cache.get_effective_tariff(user=user, room=room)

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