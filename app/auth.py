# app/auth.py

from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.config import settings
from app.database import get_db
from app.models import User


# =====================================================
# НАСТРОЙКИ ХЕШИРОВАНИЯ ПАРОЛЕЙ
# =====================================================

pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)


def verify_password(
        plain_password: str,
        hashed_password: str
) -> bool:
    """
    Проверка пароля
    """
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """
    Хеширование пароля
    """
    return pwd_context.hash(password)


# =====================================================
# JWT / OAUTH2
# =====================================================

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/api/token"
)



def create_access_token(data: dict) -> str:
    """
    Создание JWT токена
    """

    to_encode = data.copy()

    expire = datetime.utcnow() + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )

    to_encode.update({
        "exp": expire
    })

    encoded_jwt = jwt.encode(
        to_encode,
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM
    )

    return encoded_jwt


# =====================================================
# ТЕКУЩИЙ ПОЛЬЗОВАТЕЛЬ
# =====================================================

async def get_current_user(
        token: str = Depends(oauth2_scheme),
        db: AsyncSession = Depends(get_db)
) -> User:
    """
    Получение текущего авторизованного пользователя по JWT
    """

    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось проверить учетные данные",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM]
        )

        username: Optional[str] = payload.get("sub")

        if username is None:
            raise credentials_exception

    except JWTError:
        raise credentials_exception


    result = await db.execute(
        select(User).where(User.username == username)
    )

    user = result.scalars().first()

    if user is None:
        raise credentials_exception


    return user
