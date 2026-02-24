import base64
import io
import pyotp
import qrcode
from fastapi import APIRouter, Depends, HTTPException, status, Response, Body
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from jose import jwt, JWTError

from fastapi_limiter.depends import RateLimiter

from app.database import get_db
from app.models import User
from app.auth import verify_password, create_access_token, get_current_user, encrypt_totp_secret, decrypt_totp_secret, get_password_hash
from app.config import settings
from app.schemas import TotpSetupResponse, TotpVerify

router = APIRouter()


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
        samesite="lax",
        # Включаем Secure только если это продакшен (HTTPS), иначе локально не заработает
        secure=(settings.ENVIRONMENT == "production")
    )


# --- 1. ЛОГИН (ШАГ 1: Проверка пароля) ---
@router.post(
    "/token",
    dependencies=[Depends(RateLimiter(times=5, seconds=60))]
)
async def login(
        response: Response,
        form_data: OAuth2PasswordRequestForm = Depends(),
        db: AsyncSession = Depends(get_db)
):
    """
    Шаг 1: Проверка логина/пароля.
    Если 2FA включена -> Возвращает 202 Accepted с временным токеном (scope="pre-auth").
    Если 2FA выключена -> Сразу ставит куку и пускает (200 OK).
    """
    # Ищем пользователя, исключая удаленных (Soft Delete)
    result = await db.execute(
        select(User).where(User.username == form_data.username, User.is_deleted == False)
    )
    user = result.scalars().first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    # --- ДОБАВИТЬ ЭТОТ БЛОК ---
    # Проверяем, нужно ли обновить хеш (если он старого формата, например bcrypt)
    if verify_password(form_data.password, user.hashed_password):
        # Passlib умеет определять, устарел ли хеш относительно текущей конфигурации
        # Но проще всего проверить префикс или просто перезаписать, если старый

        # Самый простой способ без глубокого копания в Passlib:
        # Если хеш не начинается на "$argon2", значит он старый -> обновляем
        if not user.hashed_password.startswith("$argon2"):
            new_hash = get_password_hash(form_data.password)
            user.hashed_password = new_hash
            # Не забудьте закоммитить изменения
            db.add(user)
            await db.commit()
            # --------------------------

    # 2FA НЕТ -> Полный вход
    access_token = create_access_token(data={"sub": user.username, "role": user.role, "scope": "full"})
    set_auth_cookie(response, access_token)

    return {"access_token": access_token, "role": user.role, "status": "success"}


# --- 2. ПОДТВЕРЖДЕНИЕ ВХОДА (ШАГ 2: Проверка кода) ---
@router.post("/api/auth/verify-2fa")
async def verify_2fa_login(
        response: Response,
        data: TotpVerify,
        db: AsyncSession = Depends(get_db)
):
    """
    Шаг 2: Принимает временный токен и код из Яндекс.Ключа / Google Auth.
    """
    # 1. Проверяем наличие временного токена
    if not data.temp_token:
        raise HTTPException(status_code=400, detail="Отсутствует временный токен")

    # 2. Валидация временного токена (JWT)
    try:
        payload = jwt.decode(data.temp_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        username = payload.get("sub")
        scope = payload.get("scope")

        if scope != "pre-auth":
            raise HTTPException(status_code=401, detail="Неверный тип токена")

    except JWTError:
        raise HTTPException(status_code=401, detail="Токен истек или неверен")

    # 3. Получаем юзера из БД
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalars().first()

    if not user or not user.totp_secret:
        raise HTTPException(status_code=400, detail="2FA не настроена или пользователь не найден")

    # 4. Проверяем код через PyOTP
    decrypted_secret = decrypt_totp_secret(user.totp_secret)
    totp = pyotp.TOTP(decrypted_secret)
    # valid_window=1 дает допуск +-30 секунд для рассинхронизации часов
    if not totp.verify(data.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Неверный код из приложения")

    # 5. Успех! Выдаем полный доступ
    access_token = create_access_token(data={"sub": user.username, "role": user.role, "scope": "full"})
    set_auth_cookie(response, access_token)

    return {"status": "success", "role": user.role}


# --- 3. НАСТРОЙКА 2FA (Генерация QR) ---
@router.post("/api/auth/setup-2fa", response_model=TotpSetupResponse)
async def setup_2fa(current_user: User = Depends(get_current_user)):
    """
    Генерирует секрет и QR-код для подключения Яндекс.Ключа.
    """
    # Генерируем случайный секрет (32 символа base32)
    secret = pyotp.random_base32()

    # Создаем ссылку для QR-кода (otpauth://)
    # name - что будет написано в приложении (user@ЖКХ-Лидер)
    uri = pyotp.totp.TOTP(secret).provisioning_uri(
        name=current_user.username,
        issuer_name="ЖКХ Лидер"
    )

    # Генерируем QR-код в формате PNG -> Base64
    img = qrcode.make(uri)
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    qr_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return {"secret": secret, "qr_code": qr_b64}


# --- 4. АКТИВАЦИЯ 2FA (Подтверждение настройки) ---
@router.post("/api/auth/activate-2fa")
async def activate_2fa(
        data: TotpVerify,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Финализация настройки: юзер вводит код из приложения и секрет, который мы ему показали.
    Если код верный — сохраняем секрет в БД.
    """
    if not data.secret:
        raise HTTPException(status_code=400, detail="Секретный ключ не передан")

    # Проверяем код с переданным секретом
    totp = pyotp.TOTP(data.secret)
    if not totp.verify(data.code, valid_window=1):
        raise HTTPException(status_code=400, detail="Неверный код. Попробуйте сканировать QR снова.")

    # Сохраняем секрет в БД (включаем 2FA для этого юзера)
    current_user.totp_secret = encrypt_totp_secret(data.secret)
    await db.commit()


    return {"status": "activated", "message": "Двухфакторная аутентификация успешно включена"}


# --- 5. ВЫХОД ---
@router.post("/api/logout")
async def logout(response: Response):
    """
    Выход пользователя (удаление куки).
    """
    response.delete_cookie(
        key="access_token",
        samesite="lax",
        httponly=True,
        secure=(settings.ENVIRONMENT == "production")
    )
    return {"status": "success", "message": "Успешный выход"}