from fastapi import APIRouter, Depends, HTTPException, status
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
        form_data: OAuth2PasswordRequestForm = Depends(),
        db: AsyncSession = Depends(get_db)
):
    """
    Авторизация пользователя и выдача JWT токена
    """

    result = await db.execute(
        select(User).where(User.username == form_data.username)
    )

    user = result.scalars().first()


    if not user or not verify_password(
            form_data.password,
            user.hashed_password
    ):
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


    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        "role": user.role
    }
