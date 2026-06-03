from sqlalchemy.ext.asyncio import AsyncSession
from app.modules.utility.models import User


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
    room_id = user.room_id

    # Освобождаем И ФИО (username), И логин — чтобы в будущем можно было
    # зарегистрировать нового жильца с таким же ФИО/логином (индексы глобальные).
    user.username = f"{user.username}_deleted_{user.id}"
    user.login = f"{user.login}_deleted_{user.id}"

    db.add(user)
    await db.flush()

    # Холостяцкая комната: пересчитать делитель счётчиков (жилец выбыл).
    from app.modules.utility.services.room_assignment import recount_singles_residents
    await recount_singles_residents(db, room_id)
    # db.commit() будет вызван в роутере
