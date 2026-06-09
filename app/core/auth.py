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
    Извлекает токен из Authorization: Bearer <token>.

    ИСПРАВЛЕНИЕ MIXING-БАГА (apr 2026):
    Раньше был fallback на HttpOnly Cookie access_token, что приводило
    к смешиванию сессий между разными пользователями на одном устройстве:

      1. User A логинится → cookie (max_age=120 мин, browser-wide) +
         sessionStorage (per-tab).
      2. User A закрывает tab — sessionStorage умирает, cookie живёт
         ещё 2 часа.
      3. На том же физическом устройстве User B открывает портал в
         новой вкладке → пустой sessionStorage → JS не отправляет
         Authorization → backend читает cookie от A → User B видит
         данные User A.

    Cookie auth уже не нужен:
    - Frontend (api.js) ВСЕГДА шлёт Bearer header из sessionStorage.
    - Mobile Flutter app использует только Bearer.
    - Swagger UI отключён в production (docs_url=None).
    - Direct PDF/file links все идут через api.download (тот же header).

    Если захотим вернуть cookie-auth для XSS hardening — это будет
    отдельная задача (см. этап 2A/2B плана) с правильным lifecycle:
    cookie set/delete синхронизированы с sessionStorage, и НЕТ
    fallback при отсутствии header — авторизация одна, а не две.
    """

    async def __call__(self, request: Request) -> Optional[str]:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            if token:
                return token

        # Стандартный OAuth2 (для Swagger UI в dev — в production Swagger
        # отключён, так что это путь не сработает).
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

        sub: Optional[str] = payload.get("sub")
        token_role: Optional[str] = payload.get("role")
        token_scope: Optional[str] = payload.get("scope", "full")
        # tv (token version) — int счётчик, инкрементируется при logout /
        # change-password / pdn-consent-revoke. Старые токены становятся
        # сразу невалидными. См. миграцию token_001_version.
        token_tv: Optional[int] = payload.get("tv")

        if sub is None:
            raise credentials_exception

        # pre-auth токены выдаются после ввода пароля, но ДО 2FA —
        # они дают доступ только к /api/auth/verify-2fa. На все остальные
        # защищённые endpoints они не должны работать.
        if token_scope != "full":
            raise credentials_exception

    except JWTError:
        raise credentials_exception

    # Резолвим юзера по sub. Новые токены несут sub = user.id (неизменяемый),
    # старые (до релиза users_login_001) — sub = username. Поддерживаем оба,
    # пока старые не истекут → НЕТ массового разлогина на деплое. Лукап по id
    # не ломает сессию при переименовании ФИО (link-fio): id стабилен.
    # Фильтр is_deleted — удалённый не имеет доступа даже с непротухшим токеном.
    try:
        result = await db.execute(
            select(User).where(User.id == int(sub), User.is_deleted.is_(False))
        )
    except (TypeError, ValueError):
        result = await db.execute(
            select(User).where(User.username == sub, User.is_deleted.is_(False))
        )

    user = result.scalars().first()

    if user is None:
        raise credentials_exception

    # Проверка, что роль в токене совпадает с ролью в БД.
    # Если админу понизили права (admin → user), его старый токен с role="admin"
    # до сих пор работал бы — теперь такой токен будет отклонён.
    if token_role and token_role != user.role:
        raise credentials_exception

    # token_version check — отзыв сессии. JWT с tv=N валиден только пока
    # user.token_version == N. Logout / change-password инкрементируют
    # счётчик → все ранее выданные токены становятся невалидными.
    # Если в токене tv отсутствует (старый токен до миграции) — считаем 0
    # для backward compat, но если в БД token_version > 0 — токен невалиден.
    current_tv = user.token_version or 0
    token_tv_int = token_tv if token_tv is not None else 0
    if token_tv_int != current_tv:
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
        try:
            return fernet.decrypt(encrypted_data.encode()).decode()
        except Exception:
            # InvalidToken: секрет повреждён или сменился ENCRYPTION_KEY.
            # Раньше пробрасывалось как 500 — отдаём понятную 400 (security-аудит).
            import logging as _l
            from fastapi import HTTPException as _HTTPException
            _l.getLogger(__name__).error(
                "[2FA] TOTP-секрет не расшифровывается (повреждён / сменился ENCRYPTION_KEY)"
            )
            raise _HTTPException(
                status_code=400,
                detail="Ошибка 2FA: секрет повреждён. Обратитесь к администратору для сброса 2FA.",
            )

    return db_secret
