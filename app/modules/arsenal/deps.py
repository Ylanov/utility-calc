from fastapi import Depends, HTTPException, Request
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from passlib.context import CryptContext

from app.core.database import get_arsenal_db
from app.core.config import settings
from app.modules.arsenal.models import ArsenalUser

# Настройка хэширования
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")

async def get_current_arsenal_user(
        request: Request,
        db: AsyncSession = Depends(get_arsenal_db)
):
    token = request.cookies.get("access_token")
    if not token:
        raise HTTPException(status_code=401, detail="Не авторизован")

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="Неверный токен")
    except JWTError:
        raise HTTPException(status_code=401, detail="Ошибка валидации токена")

    result = await db.execute(select(ArsenalUser).where(ArsenalUser.username == username))
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=401, detail="Пользователь Арсенала не найден")

    return user
