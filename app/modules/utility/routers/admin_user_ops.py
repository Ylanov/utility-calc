# app/modules/utility/routers/admin_user_ops.py

import logging
import secrets
import string
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete

from app.core.auth import get_password_hash
from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Adjustment
from app.core.dependencies import RoleChecker

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Admin User Ops"])

# ИСПРАВЛЕНИЕ: Используем RoleChecker вместо ручной проверки role != "accountant".
# Ранее роль 'admin' не могла удалять пользователей — только 'accountant'.
# Это противоречит логике всех остальных роутеров, где admin имеет полный доступ.
allow_delete_users = RoleChecker(["accountant", "admin"])

# Доступ к admin-reset пароля: те же роли, что и для удаления юзера —
# accountant (бухгалтерия принимает заявки) и admin.
allow_reset_password = RoleChecker(["accountant", "admin"])

# Длина временного пароля. 12 символов из mix [a-zA-Z0-9] = ~71 бит энтропии,
# на порядки сильнее старого 6-цифрового (≈20 бит).
_RESET_PASSWORD_LENGTH = 12
_RESET_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def _generate_temp_password() -> str:
    """Криптографически стойкий случайный пароль через `secrets`.

    Раньше использовался `random.choices(string.digits, k=6)` — `random`
    не предназначен для secrets, к тому же 6 цифр (1M комбинаций)
    реально перебирались за минуты. Теперь — `secrets.choice` (CSPRNG)
    + 12 символов alnum.
    """
    return "".join(
        secrets.choice(_RESET_PASSWORD_ALPHABET)
        for _ in range(_RESET_PASSWORD_LENGTH)
    )


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


@router.post("/api/admin/users/{user_id}/reset-password")
async def admin_reset_password(
        user_id: int,
        current_user: User = Depends(allow_reset_password),
        db: AsyncSession = Depends(get_db),
):
    """Админский сброс пароля жильца.

    Только для admin/accountant. Заменяет старый self-service сброс
    (логин + площадь + 6 цифр), который был уязвим:
      - площадь — публично известная информация;
      - 6 цифр перебирались за минуты;
      - пароль возвращался в API-ответе и попадал бы в любой логирующий
        прокси/расширение браузера.

    Новый сценарий:
      1. Жилец просит сброс через `/api/auth/reset-password` → заявка
         регистрируется в логах (anti-enumeration, без раскрытия деталей).
      2. Админ/бухгалтер перезванивает жильцу для подтверждения личности.
      3. Админ дёргает этот endpoint → получает temp_password ОДНОКРАТНО.
      4. Передаёт пароль жильцу out-of-band (по телефону / лично).

    Возврат пароля в ответе остаётся (это единственный способ его узнать),
    но теперь:
      - доступно ТОЛЬКО админу/бухгалтеру с валидным токеном;
      - пароль криптографически стойкий (12 символов alnum, secrets);
      - событие пишется в audit_log с указанием инициатора и цели;
      - при следующем входе жилец обязан сменить пароль
        (is_initial_setup_done=False).
    """
    user = await db.get(User, user_id)
    if not user or user.is_deleted:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    # Защита: главного admin сбрасывать через этот endpoint нельзя — у
    # него отдельный recovery-флоу через прямой доступ к БД (документация
    # в README). Иначе компрометация любого admin-токена даёт каскадный
    # захват superadmin'а.
    if user.username == "admin":
        raise HTTPException(
            status_code=400,
            detail="Сброс пароля главного администратора через этот endpoint запрещён",
        )

    temp_password = _generate_temp_password()
    user.hashed_password = get_password_hash(temp_password)
    user.is_initial_setup_done = False

    # Аудит — кто кому сбросил. Импорт ленивый, чтобы избежать
    # cross-router cycle (admin_dashboard импортит auth-вещи).
    try:
        from app.modules.utility.routers.admin_dashboard import write_audit_log
        await write_audit_log(
            db, current_user.id, current_user.username,
            "reset_password", "user", user.id,
            {"target_username": user.username},
        )
    except Exception:
        # audit лог — не критичный путь; не валим основную операцию.
        logger.exception("[ADMIN-RESET] audit_log write failed")

    await db.commit()

    logger.info(
        "[ADMIN-RESET] %s (id=%s) reset password for %s (id=%s)",
        current_user.username, current_user.id,
        user.username, user.id,
    )

    return {
        "status": "success",
        "message": (
            "Временный пароль сгенерирован. Передайте его жильцу "
            "лично или по телефону. При следующем входе жилец будет "
            "обязан сменить пароль."
        ),
        "username": user.username,
        "temp_password": temp_password,
    }
