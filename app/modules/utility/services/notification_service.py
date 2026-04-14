# app/modules/utility/services/notification_service.py

import os
import json
import logging
import asyncio
from typing import List, Optional

import firebase_admin
from firebase_admin import credentials, messaging
from firebase_admin.messaging import UnregisteredError

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import delete

from app.modules.utility.models import DeviceToken
from app.core.config import settings

logger = logging.getLogger(__name__)

# ======================================================
# FIREBASE INITIALIZATION
# ======================================================

def initialize_firebase():
    """
    Инициализация Firebase Admin SDK.
    Поддерживает:
    1. JSON из ENV (рекомендуется)
    2. Файл (fallback)
    """
    try:
        if firebase_admin._apps:
            return

        # --- ВАРИАНТ 1: через JSON из ENV ---
        cred_json = os.getenv("FIREBASE_CREDENTIALS_JSON")

        if cred_json:
            try:
                cred_dict = json.loads(cred_json)
                cred = credentials.Certificate(cred_dict)
                firebase_admin.initialize_app(cred)
                logger.info("Firebase инициализирован через ENV JSON.")
                return
            except Exception as e:
                logger.error(f"Ошибка парсинга FIREBASE_CREDENTIALS_JSON: {e}")

        # --- ВАРИАНТ 2: через файл ---
        cred_path = getattr(settings, "FIREBASE_CRED_PATH", None)

        if cred_path and os.path.exists(cred_path):
            cred = credentials.Certificate(cred_path)
            firebase_admin.initialize_app(cred)
            logger.info("Firebase инициализирован через файл.")
            return

        logger.warning("Firebase не настроен: нет JSON и нет файла.")

    except Exception as e:
        logger.error(f"Ошибка инициализации Firebase: {e}")


# Инициализируем при импорте
initialize_firebase()


# ======================================================
# PUSH TO ONE USER
# ======================================================

async def send_push_to_user(
    db: AsyncSession,
    user_id: int,
    title: str,
    body: str,
    data: Optional[dict] = None
):
    """
    Отправляет пуш пользователю на все его устройства.
    """

    if not firebase_admin._apps:
        logger.warning("Firebase не инициализирован. Пропуск отправки.")
        return {"status": "skipped", "message": "Firebase not initialized"}

    # 1. Получаем токены пользователя
    result = await db.execute(
        select(DeviceToken.token).where(DeviceToken.user_id == user_id)
    )
    tokens: List[str] = result.scalars().all()

    if not tokens:
        logger.info(f"У пользователя {user_id} нет токенов.")
        return {"status": "skipped", "message": "No tokens"}

    # 2. Формируем сообщение
    message = messaging.MulticastMessage(
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=data or {},
        tokens=tokens,
    )

    # 3. Отправка
    try:
        # ИСПРАВЛЕНИЕ: Выносим синхронный сетевой I/O вызов Firebase в отдельный поток,
        # чтобы не блокировать async event loop сервера.
        response = await asyncio.to_thread(messaging.send_each_multicast, message)

        logger.info(
            f"Пуши отправлены: success={response.success_count}, "
            f"failed={response.failure_count}, user_id={user_id}"
        )

        # 4. Удаляем невалидные токены
        tokens_to_delete = []

        for idx, resp in enumerate(response.responses):
            if not resp.success:
                error = resp.exception

                if isinstance(error, UnregisteredError):
                    tokens_to_delete.append(tokens[idx])

        if tokens_to_delete:
            await db.execute(
                delete(DeviceToken).where(DeviceToken.token.in_(tokens_to_delete))
            )
            await db.commit()

            logger.info(f"Удалено {len(tokens_to_delete)} невалидных токенов.")

        return {
            "status": "success",
            "success_count": response.success_count,
            "failure_count": response.failure_count,
        }

    except Exception as e:
        logger.error(f"Ошибка отправки пуша user_id={user_id}: {e}")
        return {"status": "error", "message": str(e)}


# ======================================================
# PUSH TO ALL USERS
# ======================================================

async def send_push_to_all(
    db: AsyncSession,
    title: str,
    body: str,
    data: Optional[dict] = None
):
    """
    Массовая рассылка всем пользователям.
    """

    if not firebase_admin._apps:
        logger.warning("Firebase не инициализирован. Пропуск рассылки.")
        return

    result = await db.execute(select(DeviceToken.token))
    tokens: List[str] = result.scalars().all()

    if not tokens:
        logger.info("Нет токенов для массовой рассылки.")
        return

    chunk_size = 500
    total_success = 0
    total_failed = 0

    for i in range(0, len(tokens), chunk_size):
        chunk = tokens[i:i + chunk_size]

        message = messaging.MulticastMessage(
            notification=messaging.Notification(
                title=title,
                body=body,
            ),
            data=data or {},
            tokens=chunk,
        )

        try:
            # ИСПРАВЛЕНИЕ: Синхронный запрос к Firebase оборачиваем в asyncio.to_thread
            response = await asyncio.to_thread(messaging.send_each_multicast, message)

            total_success += response.success_count
            total_failed += response.failure_count

            # Удаляем невалидные токены
            tokens_to_delete = []

            for idx, resp in enumerate(response.responses):
                if not resp.success:
                    error = resp.exception

                    if isinstance(error, UnregisteredError):
                        tokens_to_delete.append(chunk[idx])

            if tokens_to_delete:
                await db.execute(
                    delete(DeviceToken).where(DeviceToken.token.in_(tokens_to_delete))
                )
                await db.commit()

                logger.info(f"[Batch] Удалено {len(tokens_to_delete)} токенов.")

        except Exception as e:
            logger.error(f"Ошибка массовой рассылки (batch): {e}")

    logger.info(
        f"Массовая рассылка завершена: success={total_success}, failed={total_failed}"
    )