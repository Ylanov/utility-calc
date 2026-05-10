"""Сброс пароля пользователя utility-calc из CLI.

Использование на production-сервере:

    # Сгенерировать случайный пароль (24 символа) и показать ОДИН раз:
    docker compose exec web python -m app.scripts.reset_admin_password admin

    # Установить заданный пароль (НЕ сохраняйте в bash history — берите
    # из переменной окружения):
    docker compose exec -e NEW_PW="..." web python -m app.scripts.reset_admin_password admin --from-env NEW_PW

После сброса у пользователя сбрасывается флаг is_initial_setup_done,
поэтому при первом входе откроется модалка повторной настройки логина
и пароля (`/me/setup`) — это даёт возможность сменить логин «admin» на
менее предсказуемый и сразу включить 2FA.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import string
import sys
from argparse import ArgumentParser
from typing import Optional

from sqlalchemy.future import select

from app.core.auth import get_password_hash
from app.core.database import AsyncSessionLocal
from app.modules.utility.models import User


def generate_password(length: int = 24) -> str:
    """Случайный пароль из букв и цифр. 24 символа = ~143 бита энтропии."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


async def reset(username: str, password: Optional[str]) -> int:
    generated = password is None
    if generated:
        password = generate_password()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.username == username))
        user = result.scalars().first()
        if not user:
            print(f"ОШИБКА: пользователь '{username}' не найден", file=sys.stderr)
            return 2
        user.hashed_password = get_password_hash(password)
        # Флаг сброшен — при первом входе откроется модалка /me/setup,
        # пользователь сменит логин и пароль на свои.
        user.is_initial_setup_done = False
        await db.commit()

    print(f"OK: пароль пользователя '{username}' обновлён.")
    if generated:
        print("")
        print("Новый пароль (показывается ОДИН РАЗ — сохраните сейчас):")
        print("")
        print(f"    {password}")
        print("")
    print(
        "При первом входе откроется окно первичной настройки — "
        "смените логин на менее предсказуемый и включите 2FA "
        "(в профиле -> Безопасность -> подключить TOTP)."
    )
    return 0


def main() -> int:
    parser = ArgumentParser(
        description="Сброс пароля пользователя utility-calc.",
    )
    parser.add_argument("username", help="Логин пользователя (например, admin)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--password",
        help="Новый пароль (нежелательно: попадёт в bash history).",
    )
    group.add_argument(
        "--from-env",
        metavar="VAR",
        help="Имя переменной окружения, содержащей новый пароль.",
    )
    args = parser.parse_args()

    password: Optional[str] = args.password
    if args.from_env:
        password = os.environ.get(args.from_env)
        if not password:
            print(
                f"ОШИБКА: переменная окружения '{args.from_env}' пуста или не задана",
                file=sys.stderr,
            )
            return 2

    return asyncio.run(reset(args.username, password))


if __name__ == "__main__":
    raise SystemExit(main())
