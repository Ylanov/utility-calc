# app/modules/utility/routers/admin_user_ops.py

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Adjustment
from app.core.dependencies import get_current_user, RoleChecker

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin User Ops"])

# ИСПРАВЛЕНИЕ: Используем RoleChecker вместо ручной проверки role != "accountant".
# Ранее роль 'admin' не могла удалять пользователей — только 'accountant'.
# Это противоречит логике всех остальных роутеров, где admin имеет полный доступ.
allow_delete_users = RoleChecker(["accountant", "admin"])


@router.delete("/api/admin/users/{user_id}")
async def delete_user_with_cleanup(
        user_id: int,
        current_user: User = Depends(allow_delete_users),
        db: AsyncSession = Depends(get_db)
):
    """
    Полное удаление пользователя с каскадной очисткой всех связанных данных:
    - Финансовые корректировки (Adjustments)
    - Показания счетчиков (MeterReading)
    - Сама запись пользователя (User)

    Доступно для ролей 'accountant' и 'admin'.
    """
    try:
        user = await db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        # ИСПРАВЛЕНИЕ: Проверяем точное совпадение username == "admin" вместо startswith("admin").
        # Ранее startswith("admin") блокировало удаление любого пользователя,
        # чьё имя начинается с "admin" (например "admin_test", "administrator", "admin2").
        # Защищаем только суперадмина с username ровно "admin".
        if user.username == "admin":
            raise HTTPException(status_code=400, detail="Нельзя удалить главного администратора")

        # Дополнительная защита: нельзя удалить самого себя
        if user.id == current_user.id:
            raise HTTPException(status_code=400, detail="Нельзя удалить свою учётную запись")

        # Удаляем финансовые корректировки
        await db.execute(delete(Adjustment).where(Adjustment.user_id == user_id))

        # Удаляем показания счетчиков
        await db.execute(delete(MeterReading).where(MeterReading.user_id == user_id))

        # Удаляем самого пользователя
        await db.delete(user)

        # Фиксируем всё одной транзакцией
        await db.commit()

        logger.info(
            f"User {user_id} ('{user.username}') permanently deleted "
            f"with all related data by {current_user.username}"
        )

        return {
            "status": "success",
            "message": "Пользователь и все связанные данные успешно удалены"
        }

    except HTTPException:
        # HTTPException пробрасываем как есть — это штатные ошибки (404, 403, 400)
        raise
    except Exception as e:
        await db.rollback()
        logger.error(f"Critical error deleting user {user_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Внутренняя ошибка при удалении пользователя. Обратитесь к администратору."
        )