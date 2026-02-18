# app/arsenal/auth.py
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.database import get_arsenal_db
from app.arsenal.models import ArsenalUser
from app.auth import verify_password, create_access_token # Используем утилиты из общего ядра

router = APIRouter(tags=["Arsenal Auth"])

@router.post("/api/arsenal/login")
async def arsenal_login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_arsenal_db)
):
    # Ищем пользователя ТОЛЬКО в базе Арсенала
    result = await db.execute(select(ArsenalUser).where(ArsenalUser.username == form_data.username))
    user = result.scalars().first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные Арсенала"
        )

    # Создаем токен, в payload указываем scope, чтобы отличать токены
    access_token = create_access_token(
        data={
            "sub": user.username,
            "scope": "arsenal_admin"
        }
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "redirect_url": "arsenal_dashboard.html"
    }