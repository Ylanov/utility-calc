from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_db
from app.models import User
from app.schemas import UserCreate, UserResponse
from app.dependencies import get_current_user
from app.auth import get_password_hash
from app.services.excel_service import import_users_from_excel
router = APIRouter(prefix="/api/users", tags=["Users"])


@router.post("", response_model=UserResponse)
async def create_user(new_user: UserCreate, current_user: User = Depends(get_current_user),
                      db: AsyncSession = Depends(get_db)):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    existing = await db.execute(select(User).where(User.username == new_user.username))
    if existing.scalars().first():
        raise HTTPException(status_code=400, detail="Пользователь уже существует")

    db_user = User(
        username=new_user.username,
        hashed_password=get_password_hash(new_user.password),
        role=new_user.role,
        dormitory=new_user.dormitory,
        workplace=new_user.workplace,
        residents_count=new_user.residents_count,
        total_room_residents=new_user.total_room_residents,  # Сохраняем новое поле
        apartment_area=new_user.apartment_area
    )
    db.add(db_user)
    await db.commit()
    await db.refresh(db_user)
    return db_user


@router.get("", response_model=list[UserResponse])
async def read_users(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403)
    result = await db.execute(select(User).order_by(User.id))
    return result.scalars().all()


@router.post("/import_excel", summary="Массовый импорт пользователей из Excel")
async def import_users(
        file: UploadFile = File(...),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    if not file.filename.endswith(('.xlsx', '.xls')):
        raise HTTPException(status_code=400, detail="Только файлы Excel")

    content = await file.read()
    result = await import_users_from_excel(content, db)

    return result