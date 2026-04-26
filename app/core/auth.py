# app/core/auth.py

from datetime import datetime, timedelta, timezone
from typing import Optional

import sentry_sdk
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
    argon2__memory_cost=65536,
    argon2__time_cost=2,
    argon2__parallelism=2,
)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


# =====================================================
# JWT / OAUTH2 С ПОДДЕРЖКОЙ HTTPONLY COOKIES И BEARER HEADER
# =====================================================

class OAuth2PasswordBearerWithCookie(OAuth2PasswordBearer):
    """
    Извлекает токен из запроса.
    Порядок приоритета:
    1. HTTP-заголовок Authorization: Bearer <token>  (основной способ после перехода на sessionStorage)
    2. HttpOnly Cookie access_token                   (обратная совместимость / Swagger)
    """

    async def __call__(self, request: Request) -> Optional[str]:
        # 1. Заголовок Authorization (приоритет)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            if token:
                return token

        # 2. HttpOnly Cookie (fallback)
        token = request.cookies.get("access_token")
        if token:
            if token.startswith("Bearer "):
                return token.split(" ", 1)[1].strip()
            return token

        # 3. Стандартный OAuth2 (для Swagger UI)
        return await super().__call__(request)


oauth2_scheme = OAuth2PasswordBearerWithCookie(tokenUrl="/api/token")


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    """
    Создаёт JWT-токен.

    Аргументы:
        data: payload (ожидается sub, role, scope).
        expires_delta: если передан — переопределяет дефолтный срок жизни.
            Нужно для pre-auth-токенов 2FA (короткие, ~5 минут).
    """
    to_encode = data.copy()

    if expires_delta is not None:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(
            minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
        )

    to_encode.update({"exp": expire})

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
    Получение текущего авторизованного пользователя.
    Проверяет:
    - Валидность JWT токена
    - Что пользователь существует в БД
    - Что пользователь не удалён (is_deleted = False)
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
        token_role: Optional[str] = payload.get("role")
        token_scope: Optional[str] = payload.get("scope", "full")

        if username is None:
            raise credentials_exception

        # pre-auth токены выдаются после ввода пароля, но ДО 2FA —
        # они дают доступ только к /api/auth/verify-2fa. На все остальные
        # защищённые endpoints они не должны работать.
        if token_scope != "full":
            raise credentials_exception

    except JWTError:
        raise credentials_exception

    # ИСПРАВЛЕНИЕ: фильтр is_deleted — удалённый пользователь не должен иметь доступ,
    # даже если его JWT токен ещё не истёк.
    result = await db.execute(
        select(User).where(
            User.username == username,
            User.is_deleted.is_(False)
        )
    )

    user = result.scalars().first()

    if user is None:
        raise credentials_exception

    # Проверка, что роль в токене совпадает с ролью в БД.
    # Если админу понизили права (admin → user), его старый токен с role="admin"
    # до сих пор работал бы — теперь такой токен будет отклонён.
    if token_role and token_role != user.role:
        raise credentials_exception

    # Сохраняем user_id в contextvar — все логи в рамках запроса будут с
    # ним помечены, удобно фильтровать в Sentry/Loki по конкретному жильцу.
    from app.core.request_context import current_user_id
    current_user_id.set(user.id)
    try:
        sentry_sdk.set_user({"id": user.id, "username": user.username, "role": user.role})
    except Exception:
        pass  # sentry_sdk может быть не инициализирован — это нормально

    return user


# =====================================================
# ШИФРОВАНИЕ TOTP
# =====================================================

fernet = Fernet(settings.ENCRYPTION_KEY.encode())


def encrypt_totp_secret(secret: str) -> str:
    """Шифрует TOTP секрет и добавляет маркер 'enc:'"""
    encrypted_bytes = fernet.encrypt(secret.encode())
    return f"enc:{encrypted_bytes.decode()}"


def decrypt_totp_secret(db_secret: str) -> str:
    """Расшифровывает секрет. Если маркера 'enc:' нет — возвращает как есть (обратная совместимость)."""
    if not db_secret:
        return None

    if db_secret.startswith("enc:"):
        encrypted_data = db_secret[4:]
        return fernet.decrypt(encrypted_data.encode()).decode()

    return db_secret