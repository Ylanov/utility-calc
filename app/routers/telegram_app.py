# app/routers/telegram_app.py
import json
import hashlib
import hmac
import logging
from urllib.parse import unquote
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database import get_db
from app.models import User
from app.auth import verify_password, create_access_token
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/tg", tags=["Telegram Mini App"])


class TgLoginRequest(BaseModel):
    initData: str  # Зашифрованные данные от Telegram
    username: str
    password: str


class TgAutoLoginRequest(BaseModel):
    initData: str


def validate_telegram_data(init_data: str) -> dict:
    """Проверяет криптографическую подпись Telegram."""

    # 1. Помощь для локальной разработки без Телеграма
    if not init_data or init_data == "TEST":
        if settings.ENVIRONMENT == "development":
            return {"id": "TEST_TG_ID_123"}
        else:
            raise HTTPException(status_code=401, detail="Отсутствуют данные Telegram (initData)")

    # 2. Проверка токена бота в настройках
    if not hasattr(settings, "TELEGRAM_BOT_TOKEN") or not settings.TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не задан в .env файле!")
        raise HTTPException(status_code=500, detail="Ошибка конфигурации сервера")

    try:
        # Разбираем строку от Telegram
        parsed_data = dict(qc.split("=") for qc in unquote(init_data).split("&"))

        if "hash" not in parsed_data:
            raise ValueError("Отсутствует hash в данных Telegram")

        hash_to_check = parsed_data.pop("hash")

        # Сортируем ключи по алфавиту для подписи
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))

        # Создаем секретный ключ из токена бота, который лежит в .env
        secret_key = hmac.new(b"WebAppData", settings.TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        # Сравниваем хэши
        if calculated_hash != hash_to_check:
            raise ValueError("Неверная подпись (возможно, попытка подделки данных)")

        # Извлекаем JSON пользователя
        user_data = json.loads(parsed_data.get("user", "{}"))
        return user_data

    except Exception as e:
        logger.warning(f"Ошибка валидации Telegram: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ошибка проверки подлинности Telegram"
        )


@router.post("/auto-login")
async def tg_auto_login(data: TgAutoLoginRequest, db: AsyncSession = Depends(get_db)):
    """Попытка войти автоматически по Telegram ID."""
    tg_user = validate_telegram_data(data.initData)
    tg_id = str(tg_user.get("id"))

    # Ищем пользователя с таким telegram_id
    result = await db.execute(select(User).where(User.telegram_id == tg_id, User.is_deleted == False))
    user = result.scalars().first()

    if not user:
        # Пользователь еще не привязан, фронтенд должен показать форму логина
        raise HTTPException(status_code=404, detail="Аккаунт не привязан")

    # Выдаем обычный JWT токен
    access_token = create_access_token({"sub": user.username, "role": user.role, "scope": "full"})
    return {"access_token": access_token, "role": user.role, "username": user.username}


@router.post("/login-and-link")
async def tg_login_and_link(data: TgLoginRequest, db: AsyncSession = Depends(get_db)):
    """Первый вход по логину/паролю с привязкой Telegram ID."""
    tg_user = validate_telegram_data(data.initData)
    tg_id = str(tg_user.get("id"))

    # Ищем пользователя по логину
    result = await db.execute(select(User).where(User.username == data.username, User.is_deleted == False))
    user = result.scalars().first()

    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    # Привязываем Telegram ID к аккаунту
    user.telegram_id = tg_id
    db.add(user)
    await db.commit()

    # Авторизуем
    access_token = create_access_token({"sub": user.username, "role": user.role, "scope": "full"})
    return {"access_token": access_token, "role": user.role, "username": user.username}