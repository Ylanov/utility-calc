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


# =====================================================================
# Bug Y: переселение жильца в другую комнату
# =====================================================================
allow_move = RoleChecker(["accountant", "admin"])


@router.post("/api/admin/users/{user_id}/move-to-room")
async def admin_move_user_to_room(
    user_id: int,
    new_room_id: int | None = None,
    note: str | None = None,
    current_user: User = Depends(allow_move),
    db: AsyncSession = Depends(get_db),
):
    """Переселяет жильца в другую комнату.

    Use case: Шиян переехал 4дв.стр.5/504 → 4дв.стр.5/212. Раньше админ
    просто менял User.room_id — история переездов терялась, импорты ОСВ
    за прошлые месяцы не могли понять, где жил человек тогда.

    Теперь:
      1) Закрывается активная RoomAssignment (moved_out_at=now);
      2) Создаётся новая открытая запись для new_room_id;
      3) User.room_id обновляется;
      4) Если в старой комнате никого не осталось — Room.is_vacant=True
         (комната не удаляется, история reading'ов сохраняется);
      5) Если новая комната была is_vacant — флаг снимается.

    new_room_id=None означает «выселить, никуда не заселять» (увольнение).
    """
    user = await db.get(User, user_id)
    if not user or user.is_deleted:
        raise HTTPException(404, "Пользователь не найден")

    from app.modules.utility.services.room_assignment import move_user_to_room
    closed, opened = await move_user_to_room(
        db, user=user, new_room_id=new_room_id, note=note,
    )

    # Audit log.
    try:
        from app.modules.utility.routers.admin_dashboard import write_audit_log
        await write_audit_log(
            db, current_user.id, current_user.username,
            "move_user", "user", user.id,
            {
                "from_room_id": closed.room_id if closed else None,
                "to_room_id": new_room_id,
                "note": note,
            },
        )
    except Exception:
        logger.exception("[ADMIN-MOVE] audit_log write failed")

    await db.commit()
    return {
        "status": "success",
        "user_id": user.id,
        "username": user.username,
        "moved_from": closed.room_id if closed else None,
        "moved_to": new_room_id,
        "assignment_id_closed": closed.id if closed else None,
        "assignment_id_opened": opened.id if opened else None,
    }


@router.get("/api/admin/users/{user_id}/residency-history")
async def admin_user_residency_history(
    user_id: int,
    current_user: User = Depends(allow_move),
    db: AsyncSession = Depends(get_db),
):
    """История проживания жильца — все его RoomAssignment записи.

    Возвращает массив {room_id, dormitory_name, room_number, moved_in_at,
    moved_out_at, note} в порядке от свежих к старым.
    """
    user = await db.get(User, user_id)
    if not user or user.is_deleted:
        raise HTTPException(404, "Пользователь не найден")

    from app.modules.utility.services.room_assignment import get_user_history
    from app.modules.utility.models import Room as _Room
    history = await get_user_history(db, user_id)
    items = []
    for h in history:
        room = await db.get(_Room, h.room_id)
        items.append({
            "id": h.id,
            "room_id": h.room_id,
            "dormitory_name": room.dormitory_name if room else None,
            "room_number": room.room_number if room else None,
            "moved_in_at": h.moved_in_at.isoformat() if h.moved_in_at else None,
            "moved_out_at": h.moved_out_at.isoformat() if h.moved_out_at else None,
            "is_current": h.moved_out_at is None,
            "note": h.note,
        })
    return {"user_id": user_id, "history": items}


@router.get("/api/admin/users/move-candidates")
async def admin_user_move_candidates(
    year: int | None = None,
    current_user: User = Depends(allow_move),
    db: AsyncSession = Depends(get_db),
):
    """Жильцы у которых в GSheets-подачах за период (по умолчанию текущий
    год) есть подачи в комнатах ОТЛИЧНЫХ от их текущей room_id.

    Use case: Шиян переехал 504 → 212, но админ забыл оформить переезд
    в системе. В GSheets за фев лежит подача из 504, за март/апрель/май
    из 212. Этот endpoint найдёт его и предложит сделать переезд
    официально через move-to-room.

    Возвращает для каждого жильца:
      user_id, fio, current_room (id + dormitory/number),
      seen_rooms[] — список комнат в которых встречалась подача,
      rows_in_current — сколько подач в текущей,
      rows_in_other  — сколько в других.
    """
    from datetime import datetime
    from app.modules.utility.models import (
        GSheetsImportRow,
        Room,
    )
    from sqlalchemy import select, and_
    if year is None:
        year = datetime.now().year
    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)

    # Все matched-строки за год.
    rows = (await db.execute(
        select(GSheetsImportRow).where(
            and_(
                GSheetsImportRow.sheet_timestamp >= start,
                GSheetsImportRow.sheet_timestamp < end,
                GSheetsImportRow.matched_user_id.is_not(None),
                GSheetsImportRow.matched_room_id.is_not(None),
                GSheetsImportRow.status != "rejected",
            )
        )
    )).scalars().all()

    # Группируем по user_id, собираем set комнат.
    by_user: dict[int, set] = {}
    rows_per_room: dict[tuple[int, int], int] = {}  # (uid, room_id) -> count
    for r in rows:
        by_user.setdefault(r.matched_user_id, set()).add(r.matched_room_id)
        rows_per_room[(r.matched_user_id, r.matched_room_id)] = (
            rows_per_room.get((r.matched_user_id, r.matched_room_id), 0) + 1
        )

    # Фильтруем: подача была в ≥2 разных комнатах ИЛИ единственная комната
    # отличается от current_room жильца.
    candidates = []
    uids = list(by_user.keys())
    if uids:
        users_q = (await db.execute(
            select(User).where(User.id.in_(uids))
        )).scalars().all()
        users_by_id = {u.id: u for u in users_q}

        room_ids_all = set()
        for room_set in by_user.values():
            room_ids_all.update(room_set)
        for u in users_q:
            if u.room_id:
                room_ids_all.add(u.room_id)
        rooms_q = (await db.execute(
            select(Room).where(Room.id.in_(room_ids_all))
        )).scalars().all() if room_ids_all else []
        rooms_by_id = {r.id: r for r in rooms_q}

        for uid, room_set in by_user.items():
            user = users_by_id.get(uid)
            if not user:
                continue
            current_room_id = user.room_id
            # «Кандидат» если есть подачи НЕ из текущей комнаты.
            seen_rooms = sorted(room_set)
            has_other = any(rid != current_room_id for rid in seen_rooms)
            if not has_other:
                continue
            current_room = rooms_by_id.get(current_room_id) if current_room_id else None
            seen_rooms_info = []
            for rid in seen_rooms:
                room = rooms_by_id.get(rid)
                seen_rooms_info.append({
                    "room_id": rid,
                    "dormitory_name": room.dormitory_name if room else None,
                    "room_number": room.room_number if room else None,
                    "row_count": rows_per_room.get((uid, rid), 0),
                    "is_current": rid == current_room_id,
                })
            # Сортируем seen_rooms_info: сначала текущая, потом по row_count desc
            seen_rooms_info.sort(key=lambda r: (not r["is_current"], -r["row_count"]))
            candidates.append({
                "user_id": uid,
                "username": user.username,
                "full_name": user.full_name,
                "current_room": {
                    "id": current_room.id if current_room else None,
                    "dormitory_name": current_room.dormitory_name if current_room else None,
                    "room_number": current_room.room_number if current_room else None,
                } if current_room else None,
                "seen_rooms": seen_rooms_info,
                "rows_total": sum(r["row_count"] for r in seen_rooms_info),
            })

    # Сортируем кандидатов: больше всего подач в "другой" комнате — выше.
    candidates.sort(
        key=lambda c: sum(r["row_count"] for r in c["seen_rooms"] if not r["is_current"]),
        reverse=True,
    )

    return {
        "year": year,
        "count": len(candidates),
        "candidates": candidates,
    }


@router.get("/api/admin/rooms/vacant")
async def admin_vacant_rooms(
    current_user: User = Depends(allow_move),
    db: AsyncSession = Depends(get_db),
):
    """Список всех вакантных комнат (is_vacant=True или нет привязанных
    активных жильцов). Полезно для админа: «куда селить нового жильца»."""
    from app.modules.utility.models import Room as _Room
    from sqlalchemy import select as _sel
    rows = (await db.execute(
        _sel(_Room).where(_Room.is_vacant.is_(True))
        .order_by(_Room.dormitory_name, _Room.room_number)
    )).scalars().all()
    return {
        "count": len(rows),
        "rooms": [
            {
                "id": r.id,
                "dormitory_name": r.dormitory_name,
                "room_number": r.room_number,
                "apartment_area": float(r.apartment_area or 0),
            }
            for r in rows
        ],
    }
