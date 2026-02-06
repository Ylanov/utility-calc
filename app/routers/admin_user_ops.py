from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.database import get_db
from app.models import User, MeterReading
from app.dependencies import get_current_user

router = APIRouter(tags=["Admin User Ops"])


@router.delete("/api/admin/users/{user_id}")
async def delete_user_with_cleanup(
        user_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Удаление пользователя с предварительным удалением всех связанных записей показаний.
    Доступно только для пользователей с ролью 'accountant'.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    try:
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        # Защита от удаления админа
        if user.username == "admin":
            raise HTTPException(status_code=400, detail="Нельзя удалить главного администратора")

        # Находим все показания пользователя
        readings_stmt = select(MeterReading).where(MeterReading.user_id == user_id)
        readings_result = await db.execute(readings_stmt)
        readings = readings_result.scalars().all()

        # Удаляем показания
        for reading in readings:
            await db.delete(reading)

        # Удаляем пользователя
        await db.delete(user)

        await db.commit()

        return {
            "status": "success",
            "message": f"Пользователь {user.username} удален вместе с {len(readings)} записями показаний"
        }

    except Exception as e:
        await db.rollback()
        print(f"Error deleting user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка удаления: {str(e)}")