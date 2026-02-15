# app/services/user_service.py
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete
from app.models import User, MeterReading, Adjustment


async def delete_user_service(user_id: int, db: AsyncSession):
    """
    Атомарное удаление пользователя со всеми зависимостями.
    """
    user = await db.get(User, user_id)
    if not user:
        raise ValueError("Пользователь не найден")

    if user.username == "admin":
        raise ValueError("Нельзя удалить суперадмина")

    # Удаляем зависимости (в одной транзакции)
    # 1. Корректировки
    await db.execute(delete(Adjustment).where(Adjustment.user_id == user_id))

    # 2. Показания
    await db.execute(delete(MeterReading).where(MeterReading.user_id == user_id))

    # 3. Сам пользователь
    await db.delete(user)

    # Commit должен делать вызвавший слой (роутер),
    # либо здесь, если мы уверены в границах транзакции.
    # Для чистоты оставим commit роутеру или используем context manager.