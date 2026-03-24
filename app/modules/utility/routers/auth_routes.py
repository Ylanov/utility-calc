import base64
import io
import pyotp
import qrcode
import random
import string
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
# ИЗМЕНЕНИЕ: Добавляем импорт Room
from app.modules.utility.models import User
from app.core.auth import verify_password, create_access_token, get_current_user, encrypt_totp_secret, \
    decrypt_totp_secret, get_password_hash
from app.core.config import settings
from app.modules.utility.schemas import TotpSetupResponse, TotpVerify

router = APIRouter()


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


# --- 1. ЛОГИН (Без изменений) ---
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

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    if not user.hashed_password.startswith("$argon2"):
        new_hash = get_password_hash(form_data.password)
        user.hashed_password = new_hash
        db.add(user)
        await db.commit()

    access_token = create_access_token(
        data={"sub": user.username, "role": user.role, "scope": "full"}
    )
    set_auth_cookie(response, access_token)

    return {"access_token": access_token, "role": user.role, "status": "success"}


# --- 2. ПОДТВЕРЖДЕНИЕ ВХОДА 2FA (Без изменений) ---
@router.post("/api/auth/verify-2fa")
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
        raise HTTPException(status_code=401, detail="Токен истек или неверен")

    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()
    if not user or not user.totp_secret:
        raise HTTPException(status_code=400, detail="2FA не настроена или пользователь не найден")

    decrypted_secret = decrypt_totp_secret(user.totp_secret)
    totp = pyotp.TOTP(decrypted_secret)
    if not totp.verify(data.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Неверный код из приложения")

    access_token = create_access_token(data={"sub": user.username, "role": user.role, "scope": "full"})
    set_auth_cookie(response, access_token)
    return {"status": "success", "role": user.role}


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


# --- 4. АКТИВАЦИЯ 2FA (Без изменений) ---
@router.post("/api/auth/activate-2fa")
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


# --- 6. СБРОС ПАРОЛЯ (КЛЮЧЕВЫЕ ИЗМЕНЕНИЯ) ---
@router.post("/api/auth/reset-password")
async def reset_password(data: PasswordResetRequest, db: AsyncSession = Depends(get_db)):
    # ИЗМЕНЕНИЕ: Загружаем пользователя вместе с его комнатой
    result = await db.execute(
        select(User).options(selectinload(User.room)).where(
            func.lower(User.username) == data.username.lower(),
            User.is_deleted.is_(False)
        )
    )
    user = result.scalars().first()

    if not user:
        raise HTTPException(status_code=404, detail="Пользователь с таким логином не найден")

    # ИЗМЕНЕНИЕ: Проверяем, что пользователь привязан к комнате
    if not user.room:
        raise HTTPException(
            status_code=400,
            detail="Пользователь не привязан к помещению. Сброс пароля невозможен."
        )

    # ИЗМЕНЕНИЕ: Берем площадь из user.room.apartment_area
    db_area = round(float(user.room.apartment_area or 0), 1)
    input_area = round(float(data.apartment_area), 1)

    if db_area != input_area:
        raise HTTPException(
            status_code=400,
            detail="Данные не совпадают. Проверьте площадь в квитанции или обратитесь в бухгалтерию."
        )

    temp_password = ''.join(random.choices(string.digits, k=6))

    user.hashed_password = get_password_hash(temp_password)
    user.is_initial_setup_done = False

    await db.commit()

    return {
        "status": "success",
        "message": "Пароль успешно сброшен",
        "temp_password": temp_password
    }
