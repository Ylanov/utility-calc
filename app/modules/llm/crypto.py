"""crypto.py — Fernet-шифрование токена LLM-провайдера.

Ключ берётся из env-переменной LLM_SECRET_KEY (base64-encoded 32 bytes).
Если ключа нет — функции возвращают None и плагин LLM работает в режиме
disabled (в UI красная плашка «настройте LLM_SECRET_KEY в .env»).

Зачем шифровать:
  Токен GigaChat даёт доступ к платному API на ваши деньги. Если БД
  утечёт (бэкап, dump, скриншот) — без ключа из env токен бесполезен.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

_ENV_KEY = "LLM_SECRET_KEY"


def _get_fernet() -> Optional[Fernet]:
    """Возвращает Fernet-инстанс или None если ключ не задан."""
    key = os.environ.get(_ENV_KEY)
    if not key:
        return None
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except Exception as e:
        logger.warning("[llm.crypto] Invalid %s: %s", _ENV_KEY, e)
        return None


def encrypt_token(plaintext: str) -> Optional[str]:
    """Шифрует токен. Возвращает str (base64) или None при отсутствии ключа.

    None из вызывающего кода интерпретируется как «не сохранять, попросить
    админа настроить LLM_SECRET_KEY».
    """
    f = _get_fernet()
    if f is None:
        return None
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_token(ciphertext: Optional[str]) -> Optional[str]:
    """Расшифровывает токен. Возвращает str или None при ошибке/отсутствии."""
    if not ciphertext:
        return None
    f = _get_fernet()
    if f is None:
        return None
    try:
        return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        logger.warning("[llm.crypto] InvalidToken on decrypt — wrong key?")
        return None


def is_crypto_ready() -> bool:
    """True если ключ настроен (для UI status-плашки)."""
    return _get_fernet() is not None


def generate_new_key() -> str:
    """Генерирует свежий ключ (для one-off setup-скрипта).

    Используется когда админ инициализирует пилот:
      python -c "from app.modules.llm.crypto import generate_new_key; print(generate_new_key())"
    Полученный ключ добавляется в .env как LLM_SECRET_KEY=...
    """
    return Fernet.generate_key().decode("utf-8")


__all__ = [
    "encrypt_token", "decrypt_token",
    "is_crypto_ready", "generate_new_key",
]
