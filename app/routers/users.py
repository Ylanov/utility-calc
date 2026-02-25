from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, asc, desc, func
from typing import Optional
from pydantic import BaseModel

from app.database import get_db
from app.models import User
from app.schemas import UserCreate, UserResponse, UserUpdate, PaginatedResponse
from app.dependencies import get_current_user, RoleChecker
# ДОБАВЛЕНО: импортируем verify_password для проверки старого пароля
from app.auth import get_password_hash, verify_password
from app.services.excel_service import import_users_from_excel
from app.services.user_service import delete_user_service

router = APIRouter(prefix="/api/users", tags=["Users"])

# Зависимости для проверки ролей
allow_accountant = RoleChecker(["accountant", "admin"])
allow_fin_acc = RoleChecker(["financier", "accountant", "admin"])


# =================================================================
# СХЕМЫ ДЛЯ НАСТРОЙКИ ПРОФИЛЯ
# =================================================================
class ChangeCredentials(BaseModel):
    new_username: Optional[str] = None
    new_password: Optional[str] = None
    old_password: Optional[str] = None


# =================================================================
# СПЕЦИАЛЬНЫЕ МАРШРУТЫ (ДОЛЖНЫ БЫТЬ ВНАЧАЛЕ)
# =================================================================

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Получение профиля текущего пользователя."""
    return current_user


@router.post("/me/setup")
async def initial_setup(
        data: ChangeCredentials,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(get_current_user)
):
    """
    Единоразовая смена логина и/или пароля при первом входе.
    Если пользователь оставляет старый логин (передает пустые поля),
    флаг все равно переключается, чтобы окно больше не появлялось.
    """
    # Защита: обычный жилец может сделать это только один раз
    if current_user.is_initial_setup_done and current_user.role == "user":
        raise HTTPException(
            status_code=400,
            detail="Первичная настройка уже пройдена. Логин можно изменить только через администратора."
        )

    # Если передан новый логин и он отличается от текущего
    if data.new_username and data.new_username != current_user.username:
        # Проверка, не занят ли логин (без учета регистра)
        existing_check = await db.execute(
            select(User).where(func.lower(User.username) == func.lower(data.new_username))
        )
        if existing_check.scalars().first():
            raise HTTPException(status_code=400, detail="Этот логин уже занят другим пользователем")
        current_user.username = data.new_username

    # Если передан новый пароль
    if data.new_password:
        current_user.hashed_password = get_password_hash(data.new_password)

    # Помечаем, что настройка пройдена
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
    """Смена пароля из профиля (доступна всегда и для всех ролей)"""
    if not data.old_password or not data.new_password:
        raise HTTPException(status_code=400, detail="Необходимо указать старый и новый пароль")

    if not verify_password(data.old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Неверный текущий пароль")

    current_user.hashed_password = get_password_hash(data.new_password)
    db.add(current_user)
    await db.commit()

    return {"status": "success", "message": "Пароль успешно изменен"}


@router.post("/import_excel", summary="Массовый импорт пользователей из Excel",
             dependencies=[Depends(allow_accountant)])
async def import_users(file: UploadFile = File(...), db: AsyncSession = Depends(get_db)):
    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Поддерживаются только файлы Excel (.xlsx, .xls)")

    # Проверка Magic Numbers
    header = await file.read(8)
    await file.seek(0)  # Обязательно возвращаем курсор в начало файла

    if not (header.startswith(b"PK\x03\x04") or header.startswith(b"\xd0\xcf\x11\xe0")):
        raise HTTPException(status_code=400,
                            detail="Файл поврежден или содержит вредоносный код (Неверная сигнатура Excel)")

    content = await file.read()
    result = await import_users_from_excel(content, db)
    return result


# =================================================================
# ОБЩИЕ МАРШРУТЫ (CRUD)
# =================================================================

@router.post("", response_model=UserResponse, dependencies=[Depends(allow_accountant)])
async def create_user(
        new_user: UserCreate,
        db: AsyncSession = Depends(get_db)
):
    """Создание нового пользователя."""

    # Регистро-независимая проверка на существование пользователя
    existing_check_query = select(User).where(
        func.lower(User.username) == func.lower(new_user.username)
    )
    existing_result = await db.execute(existing_check_query)
    if existing_result.scalars().first():
        raise HTTPException(status_code=400, detail="Пользователь с таким логином уже существует")

    db_user = User(
        username=new_user.username,
        hashed_password=get_password_hash(new_user.password),
        role=new_user.role,
        dormitory=new_user.dormitory,
        workplace=new_user.workplace,
        residents_count=new_user.residents_count,
        total_room_residents=new_user.total_room_residents,
        apartment_area=new_user.apartment_area,
        # По умолчанию админ создает юзера, которому еще предстоит настройка
        is_initial_setup_done=False
    )

    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)

    return db_user


@router.get("", response_model=PaginatedResponse[UserResponse], dependencies=[Depends(allow_fin_acc)])
async def read_users(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=1000),
        search: Optional[str] = Query(None),
        sort_by: str = Query("id"),
        sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
        db: AsyncSession = Depends(get_db)
):
    """Получение списка пользователей с пагинацией, поиском и сортировкой."""

    items_query = select(User).where(User.is_deleted == False)
    count_query = select(func.count(User.id)).where(User.is_deleted == False)

    if search:
        search_filter = f"%{search}%"
        search_condition = or_(
            User.username.ilike(search_filter),
            User.dormitory.ilike(search_filter),
            User.workplace.ilike(search_filter)
        )
        items_query = items_query.where(search_condition)
        count_query = count_query.where(search_condition)

    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    valid_sort_fields = {
        "id": User.id,
        "username": User.username,
        "role": User.role,
        "dormitory": User.dormitory,
        "apartment_area": User.apartment_area,
        "workplace": User.workplace
    }

    sort_column = valid_sort_fields.get(sort_by, User.id)

    if sort_dir == "desc":
        items_query = items_query.order_by(desc(sort_column))
    else:
        items_query = items_query.order_by(asc(sort_column))

    offset = (page - 1) * limit
    items_query = items_query.offset(offset).limit(limit)

    items_result = await db.execute(items_query)
    items = items_result.scalars().all()

    return {
        "total": total,
        "page": page,
        "size": limit,
        "items": items
    }


# =================================================================
# ДИНАМИЧЕСКИЕ МАРШРУТЫ (С ПАРАМЕТРАМИ)
# =================================================================

@router.get("/{user_id}", response_model=UserResponse, dependencies=[Depends(allow_accountant)])
async def read_user(
        user_id: int,
        db: AsyncSession = Depends(get_db)
):
    """Получение информации о конкретном пользователе по ID."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


@router.put("/{user_id}", response_model=UserResponse, dependencies=[Depends(allow_accountant)])
async def update_user(
        user_id: int,
        update_data: UserUpdate,
        db: AsyncSession = Depends(get_db)
):
    """Обновление информации о пользователе (Админ/Бухгалтер)."""
    db_user = await db.get(User, user_id)
    if not db_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if update_data.username and update_data.username != db_user.username:
        existing_check_query = select(User).where(
            func.lower(User.username) == func.lower(update_data.username),
            User.id != user_id
        )
        existing_result = await db.execute(existing_check_query)
        if existing_result.scalars().first():
            raise HTTPException(status_code=400, detail="Пользователь с таким логином уже существует")

    update_dict = update_data.dict(exclude_unset=True)

    if "password" in update_dict and update_dict["password"]:
        db_user.hashed_password = get_password_hash(update_dict["password"])
        del update_dict["password"]

    for key, value in update_dict.items():
        setattr(db_user, key, value)

    await db.commit()
    await db.refresh(db_user)
    return db_user


@router.delete("/{user_id}", status_code=204, dependencies=[Depends(allow_accountant)])
async def delete_user(
        user_id: int,
        db: AsyncSession = Depends(get_db)
):
    """Удаление пользователя и всех связанных данных."""
    try:
        await delete_user_service(user_id, db)
        await db.commit()
    except ValueError as e:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=500, detail="Ошибка при удалении пользователя")

    return None