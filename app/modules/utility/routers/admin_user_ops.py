# app/modules/utility/routers/admin_user_ops.py

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Adjustment
from app.core.dependencies import get_current_user

logger = logging.getLogger(__name__)

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
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        if user.username == "admin" or (hasattr(user, 'username') and user.username.startswith("admin")):
            raise HTTPException(status_code=400, detail="Нельзя удалить главного администратора")

        # Удаляем финансовые корректировки
        await db.execute(delete(Adjustment).where(Adjustment.user_id == user_id))

        # Удаляем показания счетчиков
        await db.execute(delete(MeterReading).where(MeterReading.user_id == user_id))

        # Удаляем самого пользователя
        await db.delete(user)

        # Фиксируем всё одной транзакцией
        await db.commit()

        return {
            "status": "success",
            "message": f"Пользователь и все связанные данные успешно удалены"
        }

    except HTTPException:
        # HTTPException пробрасываем как есть — это штатные ошибки (404, 403, 400)
        raise
    except Exception as e:
        await db.rollback()
        # ИСПРАВЛЕНИЕ: логируем полную ошибку в лог, клиенту отдаём generic-сообщение
        logger.error(f"Critical error deleting user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при удалении пользователя. Обратитесь к администратору."
        )