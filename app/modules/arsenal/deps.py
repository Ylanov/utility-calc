# app/modules/arsenal/deps.py

import logging
from fastapi import Depends, HTTPException, Request
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from passlib.context import CryptContext

from app.core.database import get_arsenal_db
from app.core.config import settings
from app.modules.arsenal.models import ArsenalUser

logger = logging.getLogger(__name__)

# Настройка хэширования
pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def _extract_token(request: Request) -> str | None:
    """
    Извлекает JWT-токен из запроса.
    Порядок приоритета:
    1. HTTP-заголовок Authorization: Bearer <token>  (используется после перехода на sessionStorage)
    2. HttpOnly Cookie access_token                   (обратная совместимость)
    """
    # 1. Заголовок Authorization
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        if token:
            return token

    # 2. Cookie (обратная совместимость)
    token = request.cookies.get("access_token")
    if token:
        if token.startswith("Bearer "):
            token = token.split(" ", 1)[1].strip()
        return token

    return None


async def get_current_arsenal_user(
        request: Request,
        db: AsyncSession = Depends(get_arsenal_db)
) -> ArsenalUser:
    """
    Проверяет токен и возвращает текущего пользователя Арсенала.
    Читает токен из Authorization header (приоритет) или из cookie (fallback).
    """
    token = _extract_token(request)

    if not token:
        raise HTTPException(status_code=401, detail="Не авторизован")

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        scope: str = payload.get("scope", "")

        if not username:
            raise HTTPException(status_code=401, detail="Неверный токен: отсутствует sub")

        # P0-фикс: строгая изоляция между модулями.
        # Раньше тут было `scope not in ("arsenal_admin", "full")` — но "full"
        # выдаётся утильному пользователю, и любой утильный JWT с совпадающим
        # username получал доступ в Арсенал. Теперь — только свой scope.
        # Арсенал-логин выдаёт scope="arsenal_admin" (см. arsenal/auth.py:80).
        if scope != "arsenal_admin":
            raise HTTPException(status_code=403, detail="Недостаточно прав для доступа к Арсеналу")

    except JWTError as e:
        logger.warning(f"Arsenal JWT validation error: {e}")
        raise HTTPException(status_code=401, detail="Ошибка валидации токена")

    result = await db.execute(
        select(ArsenalUser).where(ArsenalUser.username == username)
    )
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=401, detail="Пользователь Арсенала не найден")

    return user