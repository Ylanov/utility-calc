from typing import Optional
from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from jose import JWTError, jwt

from app.core.database import get_db
from app.modules.utility.models import User
from app.core.config import settings


# 1. Создаем класс, который умеет читать токен из кук
class OAuth2PasswordBearerWithCookie(OAuth2PasswordBearer):
    async def __call__(self, request: Request) -> Optional[str]:
        # Сначала пробуем достать из куки
        token = request.cookies.get("access_token")
        if token:
            # Если токен в куке имеет префикс "Bearer ", убираем его
            if token.startswith("Bearer "):
                return token.split(" ")[1]
            return token

        # Если куки нет, пробуем стандартный способ (из заголовка)
        return await super().__call__(request)


# 2. Используем наш класс вместо стандартного
oauth2_scheme = OAuth2PasswordBearerWithCookie(tokenUrl="/token")


async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Декодируем токен
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    # Ищем пользователя в БД
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception

    return user


class RoleChecker:
    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    def __call__(self, user: User = Depends(get_current_user)):
        # Разрешаем 'admin' выполнять действия бухгалтера и финансиста
        effective_roles = self.allowed_roles.copy()
        if "accountant" in effective_roles and "admin" not in effective_roles:
            effective_roles.append("admin")

        if user.role not in effective_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Недостаточно прав"
            )
        return user


allow_accountant = RoleChecker(["accountant", "admin"])
allow_financier = RoleChecker(["financier", "accountant", "admin"])