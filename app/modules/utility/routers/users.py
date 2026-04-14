# app/modules/utility/routers/users.py

import io
import logging
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, asc, desc, func
from sqlalchemy.orm import selectinload
from typing import Optional
from pydantic import BaseModel, Field
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from fastapi.responses import StreamingResponse
from fastapi_limiter.depends import RateLimiter

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
logger = logging.getLogger(__name__)

allow_accountant = RoleChecker(["accountant", "admin"])
allow_fin_acc = RoleChecker(["financier", "accountant", "admin"])

ZERO = Decimal("0.00")


# =================================================================
# СХЕМЫ ДЛЯ НАСТРОЙКИ ПРОФИЛЯ
# =================================================================
class ChangeCredentials(BaseModel):
    new_username: Optional[str] = Field(None, min_length=3, max_length=100)
    new_password: Optional[str] = Field(None, min_length=8, max_length=128)
    old_password: Optional[str] = None


# =================================================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ: Проверка tariff_id
# =================================================================
async def _validate_tariff_id(tariff_id: Optional[int], db: AsyncSession) -> None:
    """Проверяет что тариф с указанным ID существует и активен."""
    if tariff_id is None:
        return

    tariff = await db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(
            status_code=400,
            detail=f"Тариф с ID={tariff_id} не найден в системе"
        )
    if not tariff.is_active:
        raise HTTPException(
            status_code=400,
            detail=f"Тариф '{tariff.name}' (ID={tariff_id}) деактивирован. Выберите активный тариф."
        )


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
            selectinload(User.room),
            selectinload(User.tariff)
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
            select(User).where(func.lower(User.username) == func.lower(data.new_username))
        )
        if existing_check.scalars().first():
            raise HTTPException(status_code=400, detail="Этот логин уже занят другим пользователем")
        current_user.username = data.new_username

    if data.new_password:
        current_user.hashed_password = get_password_hash(data.new_password)

    current_user.is_initial_setup_done = True
    db.add(current_user)
    await db.commit()
    return {"status": "success", "message": "Данные успешно обновлены."}


@router.post(
    "/me/change-password",
    dependencies=[Depends(RateLimiter(times=5, seconds=60))]
)
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
    existing = await db.execute(
        select(User).where(func.lower(User.username) == func.lower(new_user.username))
    )
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Пользователь с таким логином уже существует")

    if new_user.room_id:
        room_check = await db.get(Room, new_user.room_id)
        if not room_check:
            raise HTTPException(status_code=400, detail="Комната не найдена в Жилфонде")

    await _validate_tariff_id(new_user.tariff_id, db)

    db_user = User(
        username=new_user.username,
        hashed_password=get_password_hash(new_user.password),
        role=new_user.role,
        workplace=new_user.workplace,
        residents_count=new_user.residents_count,
        tariff_id=new_user.tariff_id,
        room_id=new_user.room_id,
        is_deleted=False,
        is_initial_setup_done=False
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)

    result = await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == db_user.id)
    )
    return result.scalars().first()


@router.get("", response_model=PaginatedResponse[UserResponse], dependencies=[Depends(allow_accountant)])
async def get_users(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=500),
        cursor_id: Optional[int] = Query(None, description="ID для Keyset Pagination"),
        direction: str = Query("next", pattern="^(next|prev)$", description="Направление пагинации"),
        search: Optional[str] = Query(None),
        sort_by: str = Query("id"),
        sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
        db: AsyncSession = Depends(get_db)
):
    """
    Получение списка пользователей с поддержкой умной гибридной пагинации.
    Использует Keyset Pagination (O(1)) при сортировке по ID,
    и автоматически переходит на OFFSET при использовании фильтров.
    """
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
        "id": User.id,
        "username": User.username,
        "role": User.role,
        "dormitory": Room.dormitory_name,
        "apartment_area": Room.apartment_area,
        "workplace": User.workplace
    }
    sort_column = valid_sort_fields.get(sort_by, User.id)

    if sort_by in ["dormitory", "apartment_area"] and not search:
        items_query = items_query.outerjoin(Room, User.room_id == Room.id)

    # Используем Keyset Pagination только для дефолтной сортировки по ID
    use_keyset = (sort_by == "id")

    if use_keyset and cursor_id is not None:
        if direction == "next":
            if sort_dir == "asc":
                items_query = items_query.where(User.id > cursor_id)
            else:
                items_query = items_query.where(User.id < cursor_id)
        else: # prev
            if sort_dir == "asc":
                items_query = items_query.where(User.id < cursor_id)
            else:
                items_query = items_query.where(User.id > cursor_id)
    else:
        # Fallback на OFFSET (для текстовых сортировок)
        items_query = items_query.offset((page - 1) * limit)

    # Сортировка (инверсия для Prev)
    if use_keyset and direction == "prev":
        items_query = items_query.order_by(desc(sort_column) if sort_dir == "asc" else asc(sort_column))
    else:
        items_query = items_query.order_by(asc(sort_column) if sort_dir == "asc" else desc(sort_column))

    items_query = items_query.limit(limit)
    items = list((await db.execute(items_query)).scalars().all())

    # Возврат массива в правильном порядке при движении назад
    if use_keyset and direction == "prev":
        items.reverse()

    return {"total": total, "page": page, "size": limit, "items": items}


@router.get("/{user_id}", response_model=UserResponse, dependencies=[Depends(allow_accountant)])
async def read_user(user_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == user_id)
    )
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


@router.put("/{user_id}", response_model=UserResponse, dependencies=[Depends(allow_accountant)])
async def update_user(user_id: int, update_data: UserUpdate, db: AsyncSession = Depends(get_db)):
    db_user = await db.get(User, user_id)
    if not db_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    update_dict = update_data.dict(exclude_unset=True)

    if "room_id" in update_dict and update_dict["room_id"]:
        room_check = await db.get(Room, update_dict["room_id"])
        if not room_check:
            raise HTTPException(status_code=400, detail="Комната не найдена в Жилфонде")

    if "tariff_id" in update_dict:
        await _validate_tariff_id(update_dict["tariff_id"], db)

    if "password" in update_dict and update_dict["password"]:
        db_user.hashed_password = get_password_hash(update_dict.pop("password"))

    for key, value in update_dict.items():
        setattr(db_user, key, value)

    await db.commit()
    await db.refresh(db_user)

    result = await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == db_user.id)
    )
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
    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active)
    )).scalars().first()

    if not active_period:
        raise HTTPException(status_code=400, detail="Нет активного периода")

    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(
            User.id == user_id,
            User.is_deleted.is_(False)
        )
    )).scalars().first()

    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    action = "evict" if data.is_eviction else "move"

    if action == "evict":
        user.is_deleted = True
        user.username = f"{user.username}_deleted_{user.id}"
        user.room_id = None
        message = "Жилец успешно выселен. Финальная квитанция сформирована."
    elif action == "move":
        new_room = await db.get(Room, data.new_room_id)
        if not new_room:
            raise HTTPException(status_code=404, detail="Новая комната не найдена")
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

    ws.append([
        "ivanov_i", "pass12345", "Общежитие №1", "101", 18.5, 2, 1,
        "HW-001", "CW-002", "EL-003", "МЧС", "Базовый тариф"
    ])
    ws.append(["", "", "Общежитие №1", "102", 20.0, 3, "", "HW-004", "CW-005", "EL-006", "", ""])

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Import_Template.xlsx"}
    )


# =================================================================
# DEVICE TOKENS (Push-уведомления)
# =================================================================
@router.post("/device-token", summary="Регистрация устройства для пушей")
async def register_device_token(
        data: DeviceTokenCreate,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(DeviceToken).where(DeviceToken.token == data.token))
    existing_token = result.scalars().first()

    if existing_token:
        if existing_token.user_id != current_user.id:
            existing_token.user_id = current_user.id
            await db.commit()
    else:
        new_token = DeviceToken(
            user_id=current_user.id,
            token=data.token
        )
        db.add(new_token)
        await db.commit()

    return {"status": "success", "message": "Токен устройства успешно сохранен"}