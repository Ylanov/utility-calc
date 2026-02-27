from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.modules.utility.models import User
from cryptography.fernet import Fernet
from app.core.config import settings
# =====================================================
# НАСТРОЙКИ ХЕШИРОВАНИЯ ПАРОЛЕЙ
# =====================================================

pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    deprecated="auto",
    # Настройки для Argon2
    argon2__memory_cost=65536,  # 64 MB (по умолчанию)
    argon2__time_cost=2,        # кол-во итераций (по умолчанию)
    argon2__parallelism=2,      # потоки (по умолчанию)
)

def verify_password(plain_password: str, hashed_password: str) -> bool:
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
# JWT / OAUTH2 С ПОДДЕРЖКОЙ HTTPONLY COOKIES
# =====================================================

class OAuth2PasswordBearerWithCookie(OAuth2PasswordBearer):
    """
    Кастомный класс для извлечения токена из HttpOnly Cookies.
    """

    async def __call__(self, request: Request) -> Optional:
        # 1. Сначала ищем токен в защищенной куке
        token = request.cookies.get("access_token")

        if token:
            # ЖЕЛЕЗОБЕТОННОЕ ИСПРАВЛЕНИЕ:
            # Если токен начинается с "Bearer ", мы это обрезаем.
            # Если нет - оставляем как есть.
            if token.startswith("Bearer "):
                return token.split(" ")[1]  # Берем вторую часть после пробела
            return token

        # 2. Если куки нет, проверяем заголовок (для Swagger)
        return await super().__call__(request)


oauth2_scheme = OAuth2PasswordBearerWithCookie(tokenUrl="/token")


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
    Получение текущего авторизованного пользователя
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось проверить учетные данные",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not token:
        raise credentials_exception

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


# Инициализация объекта шифрования
fernet = Fernet(settings.ENCRYPTION_KEY.encode())


def encrypt_totp_secret(secret: str) -> str:
    """Шифрует TOTP секрет и добавляет маркер 'enc:'"""
    encrypted_bytes = fernet.encrypt(secret.encode())
    return f"enc:{encrypted_bytes.decode()}"


def decrypt_totp_secret(db_secret: str) -> str:
    """Расшифровывает секрет. Если маркера 'enc:' нет - возвращает как есть (обратная совместимость)."""
    if not db_secret:
        return None

    if db_secret.startswith("enc:"):
        encrypted_data = db_secret[4:]  # Отрезаем "enc:"
        return fernet.decrypt(encrypted_data.encode()).decode()

    return db_secret  # Возвращаем сырой секрет, если он был создан до внедрения шифрования