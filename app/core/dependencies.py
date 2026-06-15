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
    Извлекает токен из Authorization: Bearer <token>.
    См. полное обоснование в app/core/auth.py — там подробно о mixing-баге,
    который заставил убрать cookie fallback. Здесь — копия логики, потому
    что эта зависимость импортируется в роутерах через dependencies.py.
    """

    async def __call__(self, request: Request) -> Optional[str]:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1].strip()
            if token:
                return token

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
        sub: str = payload.get("sub")
        if sub is None:
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
        # Аудит #14: эта (слабая) копия раньше НЕ проверяла role/tv, хотя от неё
        # зависят RoleChecker → почти все ручки. Из-за этого
        # отзыв сессии (logout/смена пароля/сброс) и понижение роли не работали.
        # Извлекаем здесь, проверяем после резолва юзера. Зеркало auth.py.
        token_role = payload.get("role")
        token_tv = payload.get("tv")
    except JWTError as e:
        logger.debug(f"JWT decode error: {e}")
        raise credentials_exception

    # Резолвим по sub: новые токены — sub = user.id (неизменяемый), старые
    # (до users_login_001) — sub = username. Оба пути, пока старые не истекут.
    # Зеркало app/core/auth.py. Фильтр is_deleted сохранён.
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

    # Понижение роли: если админу сменили role на user, его старый токен с
    # role='admin' больше не открывает admin-ручки. Старые токены без role
    # (token_role=None) не отклоняем — backward compat.
    if token_role and token_role != user.role:
        raise credentials_exception

    # Отзыв сессии: JWT с tv=N валиден, только пока user.token_version == N.
    # logout / смена пароля / админ-сброс инкрементируют счётчик → ранее
    # выданные токены становятся невалидными. Нет tv в токене (старый) → 0.
    current_tv = user.token_version or 0
    token_tv_int = token_tv if token_tv is not None else 0
    if token_tv_int != current_tv:
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
        # admin всегда проходит (даже если в allowed_roles только user — но
        # пока этот сценарий не используется; admin может всё).
        if user.role == "admin":
            return user
        if user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Недостаточно прав"
            )
        return user


# В системе осталось ДВЕ роли: admin и user (см. миграцию roles_001_simplify).
# Раньше были accountant и financier с частичным доступом — теперь они все
# слиты в admin. Алиасы ниже оставлены для обратной совместимости с уже
# написанным кодом (роутеры используют `Depends(allow_accountant)` и т.п.).
# Все они теперь = admin-only.
allow_admin = RoleChecker(["admin"])
allow_accountant = allow_admin
allow_financier = allow_admin


