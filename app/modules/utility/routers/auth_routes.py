import base64
import io
import logging
import pyotp
import qrcode
import random
import string
from datetime import datetime, timedelta
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from sqlalchemy.orm import selectinload
from jose import jwt, JWTError

from fastapi_limiter.depends import RateLimiter

from app.core.database import get_db
from app.modules.utility.models import User
from app.modules.utility.routers.admin_dashboard import write_audit_log
from app.core.auth import verify_password, create_access_token, get_current_user, encrypt_totp_secret, \
    decrypt_totp_secret, get_password_hash
from app.core.config import settings
from app.modules.utility.schemas import TotpSetupResponse, TotpVerify

router = APIRouter()
logger = logging.getLogger(__name__)

# Параметры защиты от brute-force
MAX_FAILED_LOGINS = 3
LOCK_DURATION_MINUTES = 15
PRE_AUTH_TOKEN_EXPIRE_MINUTES = 5  # Короткий временный токен для ввода 2FA


class PasswordResetRequest(BaseModel):
    username: str
    apartment_area: float  # Контрольный вопрос: площадь помещения


def set_auth_cookie(response: Response, token: str):
    """
    Устанавливает HttpOnly куку с токеном доступа.
    Автоматически включает Secure флаг в продакшене.
    """
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="strict",
        secure=(settings.ENVIRONMENT == "production")
    )


# =====================================================================
# 1. ЛОГИН
#
# ИСПРАВЛЕНИЕ: здесь было сразу два критичных бага.
#
# (A) 2FA bypass: метод сразу выдавал scope="full" даже тем, у кого
#     включена TOTP. Украденный пароль = полный доступ, 2FA была фикцией.
# (B) Отсутствие account lockout: рейтлимитер 5 попыток/60 сек позволял
#     перебирать пароль бесконечно (чередованием пауз).
#
# Теперь:
# - Если у юзера заполнен totp_secret → выдаём временный pre-auth токен
#   (живёт 5 минут, scope="pre-auth") и требуем /api/auth/verify-2fa.
# - Неверный пароль увеличивает failed_login_count.
# - После 3 неудач выставляется locked_until = now + 15 мин.
# - Успешный вход сбрасывает счётчик.
# =====================================================================
@router.post(
    "/api/token",
    dependencies=[Depends(RateLimiter(times=5, seconds=60))]
)
async def login(
        response: Response,
        form_data: OAuth2PasswordRequestForm = Depends(),
        db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(User).where(
            User.username == form_data.username,
            User.is_deleted.is_(False)
        )
    )
    user = result.scalars().first()

    # Проверяем блокировку ДО того как обрабатываем пароль —
    # чтобы заблокированные попытки не приводили к лишнему хешу argon2.
    now = datetime.utcnow()
    if user and user.locked_until and user.locked_until > now:
        remaining = int((user.locked_until - now).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=423,
            detail=f"Учётная запись временно заблокирована из-за нескольких неудачных попыток. "
                   f"Повторите через {remaining} мин."
        )

    if not user or not verify_password(form_data.password, user.hashed_password):
        # Неверный пароль — инкрементируем счётчик.
        if user is not None:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= MAX_FAILED_LOGINS:
                user.locked_until = now + timedelta(minutes=LOCK_DURATION_MINUTES)
                user.failed_login_count = 0  # обнуляем для следующей серии
                logger.warning(
                    "Account %s locked for %d min (brute-force protection)",
                    user.username, LOCK_DURATION_MINUTES,
                )
            db.add(user)
            await db.commit()
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    # Пароль верен. Сбрасываем счётчик и блокировку.
    user.failed_login_count = 0
    user.locked_until = None

    # Миграция старых паролей на argon2 (один раз за сессию).
    if not user.hashed_password.startswith("$argon2"):
        user.hashed_password = get_password_hash(form_data.password)

    # =====================================================================
    # 2FA PATH: у юзера включён TOTP — выдаём временный токен с pre-auth scope.
    # Полный доступ юзер получит только после /api/auth/verify-2fa.
    # =====================================================================
    if user.totp_secret:
        temp_token = create_access_token(
            data={"sub": user.username, "scope": "pre-auth"},
            expires_delta=timedelta(minutes=PRE_AUTH_TOKEN_EXPIRE_MINUTES),
        )
        await db.commit()
        return {
            "access_token": temp_token,
            "requires_2fa": True,
            "status": "requires_2fa",
        }

    # =====================================================================
    # Обычный путь: 2FA не настроена — выдаём полный токен.
    # =====================================================================
    user.last_login_at = now
    access_token = create_access_token(
        data={"sub": user.username, "role": user.role, "scope": "full"}
    )
    set_auth_cookie(response, access_token)

    await write_audit_log(
        db, user.id, user.username,
        action="login", entity_type="system",
        details={"role": user.role}
    )
    await db.commit()

    return {
        "access_token": access_token,
        "role": user.role,
        "status": "success",
        "requires_2fa": False,
    }


# --- 2. ПОДТВЕРЖДЕНИЕ ВХОДА 2FA ---
# Вторая стадия логина: юзер показал пароль → получил pre-auth-токен →
# теперь вводит 6-значный код из Яндекс.Ключа/Google Authenticator.
# Только после этого получает полноценный access_token с scope="full".
@router.post(
    "/api/auth/verify-2fa",
    dependencies=[Depends(RateLimiter(times=5, seconds=60))],
)
async def verify_2fa_login(
        response: Response,
        data: TotpVerify,
        db: AsyncSession = Depends(get_db)
):
    if not data.temp_token:
        raise HTTPException(status_code=400, detail="Отсутствует временный токен")
    try:
        payload = jwt.decode(data.temp_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username = payload.get("sub")
        scope = payload.get("scope")
        if scope != "pre-auth":
            raise HTTPException(status_code=401, detail="Неверный тип токена")
    except JWTError:
        raise HTTPException(status_code=401, detail="Временный токен истёк или некорректен")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()
    if not user or not user.totp_secret:
        raise HTTPException(status_code=400, detail="2FA не настроена или пользователь не найден")

    # Блокировка от brute-force по TOTP-коду (помимо рейтлимитера).
    now = datetime.utcnow()
    if user.locked_until and user.locked_until > now:
        remaining = int((user.locked_until - now).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=423,
            detail=f"Учётная запись заблокирована. Повторите через {remaining} мин."
        )

    decrypted_secret = decrypt_totp_secret(user.totp_secret)
    totp = pyotp.TOTP(decrypted_secret)
    if not totp.verify(data.code, valid_window=1):
        # Неверный код 2FA — такой же счётчик, как у пароля.
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCK_DURATION_MINUTES)
            user.failed_login_count = 0
            logger.warning("Account %s locked (2FA brute-force)", user.username)
        db.add(user)
        await db.commit()
        raise HTTPException(status_code=400, detail="Неверный код из приложения")

    # Код верен — выдаём полный токен, сбрасываем счётчик, логируем вход.
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now

    access_token = create_access_token(data={"sub": user.username, "role": user.role, "scope": "full"})
    set_auth_cookie(response, access_token)

    await write_audit_log(
        db, user.id, user.username,
        action="login", entity_type="system",
        details={"role": user.role, "mfa": True}
    )
    await db.commit()

    return {"access_token": access_token, "status": "success", "role": user.role}


# --- 3. НАСТРОЙКА 2FA (Без изменений) ---
@router.post("/api/auth/setup-2fa", response_model=TotpSetupResponse)
async def setup_2fa(current_user: User = Depends(get_current_user)):
    secret = pyotp.random_base32()
    uri = pyotp.totp.TOTP(secret).provisioning_uri(name=current_user.username, issuer_name="ЖКХ Лидер")
    img = qrcode.make(uri)
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    qr_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
    return {"secret": secret, "qr_code": qr_b64}


# --- 4. АКТИВАЦИЯ 2FA ---
# RateLimiter защищает от brute-force ПЕРВОГО корректного кода во время
# привязки 2FA (атакующий мог бы запустить перебор по свежему QR-секрету
# жертвы при MITM-атаке).
@router.post(
    "/api/auth/activate-2fa",
    dependencies=[Depends(RateLimiter(times=10, seconds=60))],
)
async def activate_2fa(
        data: TotpVerify,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if not data.secret:
        raise HTTPException(status_code=400, detail="Секретный ключ не передан")
    totp = pyotp.TOTP(data.secret)
    if not totp.verify(data.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Неверный код. Попробуйте сканировать QR снова.")
    current_user.totp_secret = encrypt_totp_secret(data.secret)
    await db.commit()
    return {"status": "activated", "message": "Двухфакторная аутентификация успешно включена"}


# --- 5. ВЫХОД (Без изменений) ---
@router.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(key="access_token", samesite="lax", httponly=True, secure=(settings.ENVIRONMENT == "production"))
    return {"status": "success", "message": "Успешный выход"}


# --- 6. СБРОС ПАРОЛЯ ---
# RateLimiter: 3 попытки в час с одного IP — защита от перебора
# "контрольного вопроса" (площадь помещения).
@router.post(
    "/api/auth/reset-password",
    dependencies=[Depends(RateLimiter(times=3, seconds=3600))],
)
async def reset_password(data: PasswordResetRequest, db: AsyncSession = Depends(get_db)):
    """Сброс пароля по логину + площади квартиры.

    Anti-enumeration: любой неверный вход (нет логина / нет комнаты /
    не совпала площадь) отдаёт ОДИН И ТОТ ЖЕ ответ 400 с общим текстом.
    Раньше мы возвращали 404 «логин не найден» vs 400 «данные не совпали»,
    что позволяло перечислять живые учётки внешним сканером — это уже
    подтверждено на asy-tk.ru.

    Pлейн-текст temp_password пока остаётся в ответе: его читает фронт
    (static/js/login.js:128) и показывает пользователю на экране сразу.
    Убирать это — отдельная задача с изменением UX (показ через
    одноразовую админ-страницу), попадёт в «жёлтый» список.
    """
    generic_fail = HTTPException(
        status_code=400,
        detail=(
            "Не удалось сбросить пароль. Проверьте логин и площадь помещения "
            "из квитанции. Если не получается — обратитесь в бухгалтерию."
        ),
    )

    result = await db.execute(
        select(User).options(selectinload(User.room)).where(
            func.lower(User.username) == data.username.lower(),
            User.is_deleted.is_(False)
        )
    )
    user = result.scalars().first()

    # Все ветки отказа — одна и та же ошибка. Это ломает user enumeration,
    # подтверждённый внешним сканированием (404 "логин не найден" vs 400).
    if not user or not user.room:
        raise generic_fail

    db_area = round(float(user.room.apartment_area or 0), 1)
    input_area = round(float(data.apartment_area), 1)
    if db_area != input_area:
        raise generic_fail

    temp_password = ''.join(random.choices(string.digits, k=6))

    user.hashed_password = get_password_hash(temp_password)
    user.is_initial_setup_done = False

    await db.commit()

    return {
        "status": "success",
        "message": "Пароль успешно сброшен",
        "temp_password": temp_password,
    }
