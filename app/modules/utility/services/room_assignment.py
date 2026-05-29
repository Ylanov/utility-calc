"""room_assignment.py — управление историей проживания.

Когда жилец переезжает / увольняется, нужно:
  1) закрыть текущую RoomAssignment (moved_out_at = now);
  2) если есть новая комната — создать новую открытую;
  3) обновить User.room_id (для быстрого доступа без JOIN).

Раньше всё это делалось напрямую SET users.room_id = X — без следов.
Теперь через единый сервис, чтобы:
  * любой переезд автоматически попал в историю;
  * UI мог показать «жил в комнате A с 01.05 по 14.06, потом в B»;
  * квитанции за прошлые периоды могли узнать, кто жил тогда.

В функциях принимаем AsyncSession. Commit делает вызывающий — это позволяет
объединить переезд с другими изменениями (например ручное переселение из
admin_user_ops) в одну транзакцию.
"""
from __future__ import annotations

from datetime import datetime
from app.core.time_utils import utcnow
from typing import Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.utility.models import RoomAssignment, User


async def recount_singles_residents(db: AsyncSession, room_id: Optional[int]) -> None:
    """Пересчитывает total_room_residents для ХОЛОСТЯЦКОЙ комнаты = число
    активных жильцов (в singles 1 аккаунт = 1 человек).

    total_room_residents — делитель счётчиков (ГВС/ХВС/электр) в billing.
    Для family-комнат НЕ трогаем: там это число ЛЮДЕЙ (один аккаунт может
    представлять семью из N), его ведёт админ вручную в форме комнаты.
    Вызывающий делает commit.
    """
    if room_id is None:
        return
    from app.modules.utility.models import Room as _Room
    room = await db.get(_Room, room_id)
    if room is None or not bool(getattr(room, "is_singles_apartment", False)):
        return
    cnt = (await db.execute(
        select(func.count(User.id)).where(
            User.room_id == room_id,
            User.is_deleted.is_(False),
            User.role == "user",
        )
    )).scalar_one()
    room.total_room_residents = int(cnt) if cnt and cnt > 0 else 1


async def get_active_assignment(
    db: AsyncSession, user_id: int
) -> Optional[RoomAssignment]:
    """Текущая открытая запись (moved_out_at IS NULL) для жильца, либо None."""
    return (await db.execute(
        select(RoomAssignment).where(
            RoomAssignment.user_id == user_id,
            RoomAssignment.moved_out_at.is_(None),
        ).limit(1)
    )).scalars().first()


async def move_user_to_room(
    db: AsyncSession,
    *,
    user: User,
    new_room_id: Optional[int],
    note: Optional[str] = None,
    when: Optional[datetime] = None,
) -> tuple[Optional[RoomAssignment], Optional[RoomAssignment]]:
    """Переносит жильца в новую комнату либо «увольняет» (new_room_id=None).

    Возвращает (closed_assignment, opened_assignment) — закрытая и открытая
    записи (любая из них может быть None: нет старой / нет новой).

    Идемпотентно: если new_room_id == текущему room_id, ничего не делает.
    """
    when = when or utcnow()

    # Шаг 1: уже там? — ничего не делаем.
    if user.room_id == new_room_id:
        active = await get_active_assignment(db, user.id)
        return None, active

    # Шаг 2: закрываем активное (если есть)
    closed = await get_active_assignment(db, user.id)
    if closed:
        closed.moved_out_at = when
        if note:
            closed.note = (closed.note or "") + f" | out: {note}"
        await db.flush()

    # Шаг 3: открываем новое (если есть куда заезжать)
    opened: Optional[RoomAssignment] = None
    if new_room_id is not None:
        opened = RoomAssignment(
            user_id=user.id,
            room_id=new_room_id,
            moved_in_at=when,
            note=note,
        )
        db.add(opened)
        await db.flush()

    # Шаг 4: обновляем быстрый указатель
    old_room_id = user.room_id
    user.room_id = new_room_id

    # Шаг 5 (Bug Y): авто-Vacant логика.
    # Если жилец уехал из old_room и там не осталось других жильцов —
    # помечаем комнату is_vacant=True (не удаляем — история reading'ов
    # должна остаться для следующего жильца, чтобы он не получил счёт
    # со счётчика с нуля).
    # Если новая комната была is_vacant — снимаем флаг (жилец заехал).
    from sqlalchemy import select as _sel, func as _func
    from app.modules.utility.models import Room as _Room
    if old_room_id and old_room_id != new_room_id:
        # Проверяем сколько жильцов осталось.
        others_count = (await db.execute(
            _sel(_func.count(User.id)).where(
                User.room_id == old_room_id,
                User.id != user.id,
                User.is_deleted.is_(False),
            )
        )).scalar_one()
        if others_count == 0:
            old_room = await db.get(_Room, old_room_id)
            if old_room:
                old_room.is_vacant = True
    if new_room_id and new_room_id != old_room_id:
        new_room = await db.get(_Room, new_room_id)
        if new_room and new_room.is_vacant:
            new_room.is_vacant = False

    # Шаг 6: sync resident_type с флагом холостяцкой квартиры.
    # Если новая комната — singles → resident_type='single'. Иначе 'family'.
    # billing_mode не трогаем (singles платят by_meter, делёж — в billing).
    if new_room_id is not None:
        target_room = await db.get(_Room, new_room_id)
        if target_room is not None:
            expected_rt = "single" if bool(getattr(target_room, "is_singles_apartment", False)) else "family"
            if user.resident_type != expected_rt:
                user.resident_type = expected_rt

    # Шаг 7: пересчёт делителя счётчиков для холостяцких комнат (старая и новая).
    # Делаем ПОСЛЕ смены room_id и flush — COUNT должен видеть актуальное
    # расселение. recount сам пропускает не-singles комнаты.
    await db.flush()
    await recount_singles_residents(db, old_room_id)
    if new_room_id != old_room_id:
        await recount_singles_residents(db, new_room_id)

    return closed, opened


async def get_user_history(
    db: AsyncSession, user_id: int, limit: int = 50
) -> list[RoomAssignment]:
    """Полная история проживания жильца, новые сверху."""
    return list((await db.execute(
        select(RoomAssignment).where(RoomAssignment.user_id == user_id)
        .order_by(RoomAssignment.moved_in_at.desc())
        .limit(limit)
    )).scalars().all())


async def get_room_residents_at(
    db: AsyncSession, room_id: int, at_date: datetime
) -> list[int]:
    """Кто проживал в комнате на конкретную дату — нужно для квитанций
    задним числом. Возвращает список user_id."""
    rows = (await db.execute(
        select(RoomAssignment.user_id).where(
            RoomAssignment.room_id == room_id,
            RoomAssignment.moved_in_at <= at_date,
            (RoomAssignment.moved_out_at.is_(None))
            | (RoomAssignment.moved_out_at >= at_date),
        )
    )).all()
    return [r[0] for r in rows]
