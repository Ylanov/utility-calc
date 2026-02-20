from fastapi import APIRouter, Depends, HTTPException, status, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from fastapi_limiter.depends import RateLimiter

from app.database import get_db
from app.models import User
from app.auth import verify_password, create_access_token
from app.config import settings

router = APIRouter()

@router.post(
    "/token",
    dependencies=[Depends(RateLimiter(times=5, seconds=60))]
)
async def login(
        response: Response,
        form_data: OAuth2PasswordRequestForm = Depends(),
        db: AsyncSession = Depends(get_db)
):
    """
    Авторизация пользователя и выдача HttpOnly JWT куки
    """
    result = await db.execute(
        select(User).where(User.username == form_data.username)
    )
    user = result.scalars().first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль"
        )

    access_token = create_access_token(
        data={
            "sub": user.username,
            "role": user.role
        }
    )

    # ИСПРАВЛЕНИЕ ЗДЕСЬ: Убрали 'Bearer ' из value
    response.set_cookie(
        key="access_token",
        value=access_token,  # <-- Было f"Bearer {access_token}", стало просто access_token
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
        secure=False
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "role": user.role
    }
# ДОБАВЛЕННЫЙ МАРШРУТ
@router.post("/api/logout") # <---- Изменили /logout на /api/logout
async def logout(response: Response):
    """
    Выход пользователя (удаление куки)
    """
    response.delete_cookie(
        key="access_token",
        samesite="lax",
        httponly=True,
        secure=False
    )
    return {"status": "success", "message": "Успешный выход"}