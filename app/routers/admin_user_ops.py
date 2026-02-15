from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.database import get_db
from app.models import User, MeterReading, Adjustment
from app.dependencies import get_current_user

router = APIRouter(tags=["Admin User Ops"])


@router.delete("/api/admin/users/{user_id}")
async def delete_user_with_cleanup(
        user_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """
    Полное удаление пользователя с каскадной очисткой всех связанных данных:
    - Финансовые корректировки (Adjustments)
    - Показания счетчиков (MeterReading)
    - Сама запись пользователя (User)

    Доступно только для роли 'accountant'.
    """
    if current_user.role != "accountant":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    try:
        # 1. Находим пользователя
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        # 2. Защита от удаления главного администратора
        if user.username == "admin":
            raise HTTPException(status_code=400, detail="Нельзя удалить главного администратора")

        # 3. Удаляем финансовые корректировки (Adjustments)
        # Важно удалить их первыми или вместе с показаниями, чтобы не нарушить целостность
        await db.execute(delete(Adjustment).where(Adjustment.user_id == user_id))

        # 4. Удаляем показания счетчиков (MeterReading)
        await db.execute(delete(MeterReading).where(MeterReading.user_id == user_id))

        # 5. Удаляем самого пользователя
        await db.delete(user)

        # 6. Фиксируем изменения одной транзакцией
        await db.commit()

        return {
            "status": "success",
            "message": f"Пользователь {user.username} и все связанные данные (показания, корректировки) успешно удалены"
        }

    except Exception as e:
        # В случае любой ошибки откатываем транзакцию целиком
        await db.rollback()
        print(f"Critial error deleting user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка удаления: {str(e)}")