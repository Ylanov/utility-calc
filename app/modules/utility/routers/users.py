# app/modules/utility/routers/users.py
import io
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, asc, desc, func
from sqlalchemy.orm import selectinload
from typing import Optional
from pydantic import BaseModel
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from fastapi.responses import StreamingResponse

from app.core.database import get_db
from app.modules.utility.models import User, Room, BillingPeriod, MeterReading, Adjustment, Tariff, DeviceToken
from app.modules.utility.schemas import (
    UserCreate, UserResponse, UserUpdate, PaginatedResponse,
    DeviceTokenCreate, RelocateUserSchema
)
from app.core.dependencies import get_current_user, RoleChecker
from app.core.auth import get_password_hash, verify_password
from app.modules.utility.services.excel_service import import_users_from_excel
from app.modules.utility.services.user_service import delete_user_service
from app.modules.utility.services.calculations import calculate_utilities

router = APIRouter(prefix="/api/users", tags=["Users"])

allow_accountant = RoleChecker(["accountant", "admin"])
allow_fin_acc = RoleChecker(["financier", "accountant", "admin"])

ZERO = Decimal("0.00")


# =================================================================
# СХЕМЫ ДЛЯ НАСТРОЙКИ ПРОФИЛЯ
# =================================================================
class ChangeCredentials(BaseModel):
    new_username: Optional[str] = None
    new_password: Optional[str] = None
    old_password: Optional[str] = None


# =================================================================
# ЛИЧНЫЙ ПРОФИЛЬ
# =================================================================
@router.get("/me", response_model=UserResponse)
async def get_me(
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(User)
        .options(
            selectinload(User.room),  # 🔥 обязательно
            selectinload(User.tariff)  # 🔥 обязательно
        )
        .where(User.id == current_user.id)
    )

    user = result.scalars().first()
    return user


@router.post("/me/setup")
async def initial_setup(
        data: ChangeCredentials,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)
):
    if current_user.is_initial_setup_done and current_user.role == "user":
        raise HTTPException(status_code=400, detail="Первичная настройка уже пройдена.")

    if data.new_username and data.new_username != current_user.username:
        existing_check = await db.execute(
            select(User).where(func.lower(User.username) == func.lower(data.new_username)))
        if existing_check.scalars().first():
            raise HTTPException(status_code=400, detail="Этот логин уже занят другим пользователем")
        current_user.username = data.new_username

    if data.new_password:
        current_user.hashed_password = get_password_hash(data.new_password)

    current_user.is_initial_setup_done = True
    db.add(current_user)
    await db.commit()
    return {"status": "success", "message": "Данные успешно обновлены."}


@router.post("/me/change-password")
async def change_password(
        data: ChangeCredentials,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)
):
    if not data.old_password or not data.new_password:
        raise HTTPException(status_code=400, detail="Необходимо указать старый и новый пароль")

    if not verify_password(data.old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Неверный текущий пароль")

    current_user.hashed_password = get_password_hash(data.new_password)
    db.add(current_user)
    await db.commit()
    return {"status": "success", "message": "Пароль успешно изменен"}


# =================================================================
# CRUD ЖИЛЬЦОВ
# =================================================================
@router.post("", response_model=UserResponse, dependencies=[Depends(allow_accountant)])
async def create_user(new_user: UserCreate, db: AsyncSession = Depends(get_db)):
    """Создание нового пользователя с привязкой к комнате по room_id."""
    existing_check = await db.execute(select(User).where(func.lower(User.username) == func.lower(new_user.username)))
    if existing_check.scalars().first():
        raise HTTPException(status_code=400, detail="Пользователь с таким логином уже существует")

    if new_user.room_id:
        room_check = await db.get(Room, new_user.room_id)
        if not room_check:
            raise HTTPException(status_code=400, detail="Указанная комната не найдена в Жилфонде")

    db_user = User(
        username=new_user.username,
        hashed_password=get_password_hash(new_user.password),
        role=new_user.role,
        workplace=new_user.workplace,
        residents_count=new_user.residents_count,
        room_id=new_user.room_id,  # Просто сохраняем ID из Жилфонда
        tariff_id=new_user.tariff_id,
        is_initial_setup_done=False
    )

    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)

    result = await db.execute(select(User).options(selectinload(User.room)).where(User.id == db_user.id))
    return result.scalars().first()


@router.get("", response_model=PaginatedResponse[UserResponse], dependencies=[Depends(allow_fin_acc)])
async def read_users(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=1000),
        search: Optional[str] = Query(None),
        sort_by: str = Query("id"),
        sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
        db: AsyncSession = Depends(get_db)
):
    """Список пользователей с подгрузкой комнат (outerjoin для поиска)."""
    items_query = select(User).options(selectinload(User.room)).where(User.is_deleted.is_(False))
    count_query = select(func.count(User.id)).where(User.is_deleted.is_(False))

    if search:
        search_filter = f"%{search}%"
        search_condition = or_(
            User.username.ilike(search_filter),
            Room.dormitory_name.ilike(search_filter),
            Room.room_number.ilike(search_filter),
            User.workplace.ilike(search_filter)
        )
        items_query = items_query.outerjoin(Room, User.room_id == Room.id).where(search_condition)
        count_query = count_query.outerjoin(Room, User.room_id == Room.id).where(search_condition)

    total = (await db.execute(count_query)).scalar_one()

    valid_sort_fields = {
        "id": User.id, "username": User.username, "role": User.role,
        "dormitory": Room.dormitory_name, "apartment_area": Room.apartment_area, "workplace": User.workplace
    }
    sort_column = valid_sort_fields.get(sort_by, User.id)

    if sort_by in ["dormitory", "apartment_area"] and not search:
        items_query = items_query.outerjoin(Room, User.room_id == Room.id)

    items_query = items_query.order_by(desc(sort_column) if sort_dir == "desc" else asc(sort_column))
    items_query = items_query.offset((page - 1) * limit).limit(limit)
    items = (await db.execute(items_query)).scalars().all()

    return {"total": total, "page": page, "size": limit, "items": items}


@router.get("/{user_id}", response_model=UserResponse, dependencies=[Depends(allow_accountant)])
async def read_user(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).options(selectinload(User.room)).where(User.id == user_id))
    user = result.scalars().first()
    if not user: raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


@router.put("/{user_id}", response_model=UserResponse, dependencies=[Depends(allow_accountant)])
async def update_user(user_id: int, update_data: UserUpdate, db: AsyncSession = Depends(get_db)):
    db_user = await db.get(User, user_id)
    if not db_user: raise HTTPException(status_code=404, detail="Пользователь не найден")

    update_dict = update_data.dict(exclude_unset=True)

    if "room_id" in update_dict and update_dict["room_id"]:
        room_check = await db.get(Room, update_dict["room_id"])
        if not room_check:
            raise HTTPException(status_code=400, detail="Комната не найдена в Жилфонде")

    if "password" in update_dict and update_dict["password"]:
        db_user.hashed_password = get_password_hash(update_dict.pop("password"))

    for key, value in update_dict.items():
        setattr(db_user, key, value)

    await db.commit()
    await db.refresh(db_user)

    result = await db.execute(select(User).options(selectinload(User.room)).where(User.id == db_user.id))
    return result.scalars().first()


@router.delete("/{user_id}", status_code=204, dependencies=[Depends(allow_accountant)])
async def delete_user(user_id: int, db: AsyncSession = Depends(get_db)):
    try:
        await delete_user_service(user_id, db)
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Ошибка при удалении")
    return None


# =================================================================
# ЕДИНОЕ ОКНО: РАЗОВОЕ НАЧИСЛЕНИЕ И ПЕРЕСЕЛЕНИЕ/ВЫСЕЛЕНИЕ
# =================================================================
@router.post("/{user_id}/relocate", dependencies=[Depends(allow_accountant)])
async def relocate_user(user_id: int, data: RelocateUserSchema, db: AsyncSession = Depends(get_db)):
    """Единый процесс: Разовое начисление по старой комнате + Переселение/Выселение"""

    active_period = (await db.execute(select(BillingPeriod).where(BillingPeriod.is_active))).scalars().first()
    if not active_period: raise HTTPException(status_code=400, detail="Нет активного периода")

    user = await db.execute(select(User).options(selectinload(User.room)).where(User.id == user_id))
    user = user.scalars().first()

    if not user or user.is_deleted: raise HTTPException(status_code=404, detail="Жилец не найден")

    old_room = user.room
    if not old_room: raise HTTPException(status_code=400, detail="Жилец не привязан к помещению, расчет невозможен")

    if data.action == "move" and not data.new_room_id:
        raise HTTPException(status_code=400, detail="Для переселения необходимо указать новую комнату")

    if data.total_days_in_month <= 0 or data.days_lived < 0 or data.days_lived > data.total_days_in_month:
        raise HTTPException(status_code=400, detail="Неверно указаны дни проживания")

    fraction = Decimal(data.days_lived) / Decimal(data.total_days_in_month)

    t = (await db.execute(select(Tariff).where(Tariff.id == getattr(user, 'tariff_id', 1)))).scalars().first() or \
        (await db.execute(select(Tariff).where(Tariff.is_active))).scalars().first()

    # Получаем историю для проверки, что счетчики не скрутили
    history = (await db.execute(
        select(MeterReading).where(MeterReading.room_id == old_room.id, MeterReading.is_approved)
        .order_by(MeterReading.created_at.desc()).limit(6)
    )).scalars().all()

    prev_latest = history[0] if history else None
    prev_manual = next((r for r in history if r.anomaly_flags != "AUTO_GENERATED"), None)

    p_hot_man = prev_manual.hot_water if prev_manual else ZERO
    p_cold_man = prev_manual.cold_water if prev_manual else ZERO
    p_elect_man = prev_manual.electricity if prev_manual else ZERO

    if data.hot_water < p_hot_man or data.cold_water < p_cold_man or data.electricity < p_elect_man:
        raise HTTPException(400, "Новые показания не могут быть меньше реально переданных ранее!")

    p_hot = prev_latest.hot_water if prev_latest else ZERO
    p_cold = prev_latest.cold_water if prev_latest else ZERO
    p_elect = prev_latest.electricity if prev_latest else ZERO

    d_hot, d_cold, d_elect = data.hot_water - p_hot, data.cold_water - p_cold, data.electricity - p_elect

    residents_count = user.residents_count if user.residents_count is not None else 1
    total_room = old_room.total_room_residents if old_room.total_room_residents > 0 else 1

    user_share_elect = (Decimal(residents_count) / Decimal(total_room)) * d_elect

    # Расчет стоимости для старой комнаты с учетом прожитых дней
    costs = calculate_utilities(
        user=user, room=old_room, tariff=t, volume_hot=d_hot, volume_cold=d_cold,
        volume_sewage=d_hot + d_cold, volume_electricity_share=user_share_elect, fraction=fraction
    )

    adj_map = {row[0]: (row[1] or ZERO) for row in
               (await db.execute(select(Adjustment.account_type, func.sum(Adjustment.amount))
                                 .where(Adjustment.user_id == user.id,
                                        Adjustment.period_id == active_period.id).group_by(
                   Adjustment.account_type))).all()}

    # Ищем, не подавал ли жилец уже черновик в этом месяце
    draft = (await db.execute(
        select(MeterReading).where(MeterReading.room_id == old_room.id, MeterReading.is_approved.is_(False),
                                   MeterReading.period_id == active_period.id)
    )).scalars().first()

    total_209 = (costs['total_cost'] - costs['cost_social_rent']) + (draft.debt_209 or ZERO if draft else ZERO) - (
        draft.overpayment_209 or ZERO if draft else ZERO) + adj_map.get('209', ZERO)
    total_205 = costs['cost_social_rent'] + (draft.debt_205 or ZERO if draft else ZERO) - (
        draft.overpayment_205 or ZERO if draft else ZERO) + adj_map.get('205', ZERO)

    # 1. ЗАПИСЬ РАСЧЕТА ЗА СТАРУЮ КОМНАТУ
    if draft:
        draft.hot_water, draft.cold_water, draft.electricity = data.hot_water, data.cold_water, data.electricity
        draft.anomaly_flags, draft.anomaly_score = "RELOCATION_CHARGE", 0
        for k, v in costs.items(): setattr(draft, k, v)
        draft.total_209, draft.total_205, draft.total_cost, draft.is_approved = total_209, total_205, total_209 + total_205, True
    else:
        costs.pop('total_cost', None)
        db.add(MeterReading(
            user_id=user.id, room_id=old_room.id, period_id=active_period.id,
            hot_water=data.hot_water, cold_water=data.cold_water, electricity=data.electricity,
            debt_209=ZERO, overpayment_209=ZERO, debt_205=ZERO, overpayment_205=ZERO,
            total_209=total_209, total_205=total_205, total_cost=total_209 + total_205,
            is_approved=True, anomaly_flags="RELOCATION_CHARGE", anomaly_score=0, **costs
        ))

    old_room.last_hot_water, old_room.last_cold_water, old_room.last_electricity = data.hot_water, data.cold_water, data.electricity
    db.add(old_room)

    # 2. ПРИМЕНЕНИЕ ДЕЙСТВИЯ К ЖИЛЬЦУ (ТРАНЗАКЦИЯ)
    if data.action == "evict":
        user.is_deleted = True
        user.username = f"{user.username}_deleted_{user.id}"
        user.room_id = None
        message = "Жилец успешно выселен. Финальная квитанция сформирована."
    elif data.action == "move":
        new_room = await db.get(Room, data.new_room_id)
        if not new_room: raise HTTPException(status_code=404, detail="Новая комната не найдена")
        user.room_id = new_room.id
        message = f"Финальная квитанция сформирована. Жилец переведен в {new_room.dormitory_name}, ком. {new_room.room_number}."

    await db.commit()
    return {"status": "success", "message": message}


# =================================================================
# ИМПОРТ И ЭКСПОРТ EXCEL
# =================================================================
@router.post("/import_excel", summary="Умный импорт (Жилфонд + Жильцы)", dependencies=[Depends(allow_accountant)])
async def import_users(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Только файлы Excel (.xlsx, .xls)")

    header = await file.read(8)
    await file.seek(0)
    if not (header.startswith(b"PK\x03\x04") or header.startswith(b"\xd0\xcf\x11\xe0")):
        raise HTTPException(status_code=400, detail="Неверная сигнатура Excel файла")

    content = await file.read()
    return await import_users_from_excel(content, db)


@router.get("/export/template", summary="Скачать шаблон для импорта")
async def download_import_template():
    wb = Workbook()
    ws = wb.active
    ws.title = "Шаблон импорта"

    headers = [
        "Логин (Оставьте пустым для создания только комнаты)",
        "Пароль (Можно пусто)", "Общежитие (ОБЯЗАТЕЛЬНО)", "Номер комнаты (ОБЯЗАТЕЛЬНО)",
        "Площадь м2", "Макс. мест в комнате", "Кол-во жильцов на Л/С",
        "№ ГВС", "№ ХВС", "№ Электр.", "Место работы", "Тарифный профиль"
    ]
    ws.append(headers)

    header_fill = PatternFill(start_color="3B82F6", end_color="3B82F6", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        ws.column_dimensions[cell.column_letter].width = 25

    ws.append(["ivanov_i", "pass123", "Общежитие №1", "101", 18.5, 2, 1, "HW-001", "CW-002", "EL-003", "МЧС",
               "Базовый тариф"])
    ws.append(["", "", "Общежитие №1", "102", 20.0, 3, "", "HW-004", "CW-005", "EL-006", "", ""])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Import_Template.xlsx"}
    )


@router.post("/device-token", summary="Регистрация устройства для Пушей")
async def register_device_token(
        data: DeviceTokenCreate,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    # Проверяем, есть ли уже такой токен в нашей PostgreSQL
    result = await db.execute(select(DeviceToken).where(DeviceToken.token == data.token))
    existing_token = result.scalars().first()

    if existing_token:
        # Если токен есть, но принадлежит другому юзеру (например, муж вышел, жена зашла с того же телефона)
        if existing_token.user_id != current_user.id:
            existing_token.user_id = current_user.id
            await db.commit()
    else:
        # Если токена нет, сохраняем его в нашу базу
        new_token = DeviceToken(
            user_id=current_user.id,
            token=data.token,
            device_type=data.device_type
        )
        db.add(new_token)
        await db.commit()

    return {"status": "success", "message": "Токен устройства успешно сохранен"}