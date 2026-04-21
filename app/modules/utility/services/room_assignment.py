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
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.utility.models import RoomAssignment, User


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
    when = when or datetime.utcnow()

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
    user.room_id = new_room_id

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
