# app/core/dependencies.py

import logging
from typing import Optional

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from jose import JWTError, jwt

from app.core.database import get_db
from app.modules.utility.models import User
from app.core.config import settings

logger = logging.getLogger(__name__)


# =====================================================
# ИЗВЛЕЧЕНИЕ ТОКЕНА: ЗАГОЛОВОК → КУКА → OAUTH2
# =====================================================

class OAuth2PasswordBearerWithCookie(OAuth2PasswordBearer):
    """
    Расширение стандартного OAuth2:
    1. Читает токен из Authorization: Bearer header (основной способ после перехода на sessionStorage)
    2. Читает токен из HttpOnly Cookie access_token (обратная совместимость)
    3. Стандартный OAuth2 fallback (Swagger UI)
    """

    async def __call__(self, request: Request) -> Optional[str]:
        # 1. Заголовок Authorization (приоритет)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            if token:
                return token

        # 2. Cookie (обратная совместимость)
        token = request.cookies.get("access_token")
        if token:
            if token.startswith("Bearer "):
                return token.split(" ", 1)[1].strip()
            return token

        # 3. Стандартный OAuth2 для Swagger
        return await super().__call__(request)


oauth2_scheme = OAuth2PasswordBearerWithCookie(tokenUrl="/api/token")


# =====================================================
# ТЕКУЩИЙ ПОЛЬЗОВАТЕЛЬ
# =====================================================

async def get_current_user(
        token: str = Depends(oauth2_scheme),
        db: AsyncSession = Depends(get_db)
) -> User:
    """
    Декодирует JWT токен и возвращает пользователя из БД.
    Проверяет:
    - Валидность и подпись JWT
    - scope == "full" (см. ниже — критично для 2FA)
    - Существование пользователя в БД
    - Что пользователь не удалён (is_deleted = False)
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Не удалось проверить учетные данные",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception

        # P0-фикс: раньше scope не проверялся. Pre-auth-токены (scope="pre-auth"),
        # которые выдаются после ввода пароля ДО ввода TOTP, проходили как
        # полноценные и открывали все защищённые роуты — 2FA фактически
        # обходился. Симметрично с app/core/auth.py:143-145.
        # Единственный валидный путь pre-auth — /api/auth/verify-2fa, и там
        # свой отдельный jwt.decode с проверкой scope=="pre-auth" (auth_routes.py:184).
        token_scope = payload.get("scope", "full")
        if token_scope != "full":
            raise credentials_exception
    except JWTError as e:
        logger.debug(f"JWT decode error: {e}")
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

    # Помечаем все логи и Sentry-events этим request'ом — user_id жильца.
    from app.core.request_context import current_user_id
    current_user_id.set(user.id)
    try:
        import sentry_sdk
        sentry_sdk.set_user({"id": user.id, "username": user.username, "role": user.role})
    except Exception:
        pass

    return user


# =====================================================
# ПРОВЕРКА РОЛЕЙ
# =====================================================

class RoleChecker:
    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    def __call__(self, user: User = Depends(get_current_user)) -> User:
        # admin всегда имеет права бухгалтера и финансиста
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