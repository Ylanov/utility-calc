from sqlalchemy.ext.asyncio import AsyncSession
from app.models import User


async def delete_user_service(user_id: int, db: AsyncSession):
    """
    МЯГКОЕ УДАЛЕНИЕ (Soft Delete).
    Мы не трогаем показания и корректировки (они нужны для истории и бухгалтерии).
    Мы просто помечаем юзера как удаленного и освобождаем его логин.
    """
    user = await db.get(User, user_id)
    if not user or user.is_deleted:
        raise ValueError("Пользователь не найден")

    if user.username == "admin":
        raise ValueError("Нельзя удалить суперадмина")

    # Помечаем как удаленного
    user.is_deleted = True

    # Меняем логин, чтобы в будущем можно было зарегистрировать нового жильца с таким же ФИО/логином
    user.username = f"{user.username}_deleted_{user.id}"

    db.add(user)
    # db.commit() будет вызван в роутере