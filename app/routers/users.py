from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import delete  # ДОБАВЛЕНО для удаления связанных записей
from app.database import get_db
from app.models import User, MeterReading  # ДОБАВЛЕНО MeterReading для обработки зависимостей
from app.schemas import UserCreate, UserResponse, UserUpdate
from app.dependencies import get_current_user
from app.auth import get_password_hash
from app.services.excel_service import import_users_from_excel

router = APIRouter(prefix="/api/users", tags=["Users"])


@router.post("", response_model=UserResponse)
async def create_user(
        new_user: UserCreate,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Создание нового пользователя.
    Доступно только для пользователей с ролью 'accountant'.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # Проверка на существующего пользователя
    existing = await db.execute(select(User).where(User.username == new_user.username))
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Пользователь уже существует")

    # Создание нового пользователя
    db_user = User(
        username=new_user.username,
        hashed_password=get_password_hash(new_user.password),
        role=new_user.role,
        dormitory=new_user.dormitory,
        workplace=new_user.workplace,
        residents_count=new_user.residents_count,
        total_room_residents=new_user.total_room_residents,
        apartment_area=new_user.apartment_area
    )

    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user


@router.get("", response_model=list[UserResponse])
async def read_users(
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Получение списка всех пользователей.
    Доступно только для пользователей с ролью 'accountant'.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    result = await db.execute(select(User).order_by(User.id))
    return result.scalars().all()


@router.get("/{user_id}", response_model=UserResponse)
async def read_user(
        user_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Получение информации о конкретном пользователе по ID.
    Доступно только для пользователей с ролью 'accountant'.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")
    return user


@router.put("/{user_id}", response_model=UserResponse)
async def update_user(
        user_id: int,
        update_data: UserUpdate,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Обновление информации о пользователе.
    Доступно только для пользователей с ролью 'accountant'.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    db_user = await db.get(User, user_id)
    if not db_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Преобразуем Pydantic модель в словарь, исключая неустановленные значения
    update_dict = update_data.dict(exclude_unset=True)

    # Особая обработка пароля
    if "password" in update_dict and update_dict["password"]:
        db_user.hashed_password = get_password_hash(update_dict["password"])
        del update_dict["password"]  # Удаляем, чтобы не попасть в цикл ниже

    # Обновляем остальные поля
    for key, value in update_dict.items():
        setattr(db_user, key, value)

    await db.commit()
    await db.refresh(db_user)
    return db_user


@router.delete("/{user_id}")
async def delete_user(
        user_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Удаление пользователя с предварительным удалением всех связанных записей показаний.
    Доступно только для пользователей с ролью 'accountant'.
    Возвращает JSON с сообщением об успешном удалении.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    # Получаем пользователя
    db_user = await db.get(User, user_id)
    if not db_user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Защита от удаления самого себя или главного админа
    if db_user.id == current_user.id or db_user.username == "admin":
        raise HTTPException(
            status_code=400,
            detail="Этого пользователя нельзя удалить"
        )

    try:
        # 1. Сначала удаляем все связанные записи показаний (readings)
        # Вариант А: Использование delete() с where (более эффективно)
        await db.execute(
            delete(MeterReading).where(MeterReading.user_id == user_id)
        )

        # Вариант Б: Через цикл (если нужна дополнительная логика обработки)
        # stmt = select(MeterReading).where(MeterReading.user_id == user_id)
        # result = await db.execute(stmt)
        # readings = result.scalars().all()
        # deleted_count = 0
        # for reading in readings:
        #     await db.delete(reading)
        #     deleted_count += 1

        # 2. Теперь удаляем самого пользователя
        await db.delete(db_user)

        # 3. Коммитим транзакцию
        await db.commit()

        return {
            "status": "success",
            "message": f"Пользователь '{db_user.username}' успешно удален",
            "deleted_user_id": user_id,
            "details": "Все связанные записи показаний также были удалены"
        }

    except Exception as e:
        # Откатываем транзакцию в случае ошибки
        await db.rollback()
        print(f"Ошибка при удалении пользователя {user_id}: {e}")

        # Проверяем, является ли ошибка нарушением внешнего ключа
        error_str = str(e).lower()
        if "foreign key" in error_str or "violates foreign key constraint" in error_str:
            raise HTTPException(
                status_code=400,
                detail="Невозможно удалить пользователя, так как существуют связанные данные. "
                       "Убедитесь, что все связанные записи были удалены."
            )

        raise HTTPException(
            status_code=500,
            detail=f"Внутренняя ошибка сервера при удалении пользователя: {str(e)}"
        )


@router.post("/import_excel", summary="Массовый импорт пользователей из Excel")
async def import_users(
        file: UploadFile = File(...),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Массовый импорт пользователей из файла Excel.
    Доступно только для пользователей с ролью 'accountant'.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Поддерживаются только файлы Excel (.xlsx, .xls)")

    # Чтение содержимого файла
    content = await file.read()

    # Импорт пользователей из Excel
    result = await import_users_from_excel(content, db)

    return result

@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """
    Получение профиля текущего пользователя
    """
    return current_user