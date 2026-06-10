"""QR-портал по квартире: токен комнаты + резолв + «представитель комнаты».

Постоянный неугадываемый токен на Room (qr_token). Анонимная подача по
ссылке /q/<token>. Показания привязаны к комнате; подачу ведёт «представитель»
— детерминированный жилец комнаты (владелец текущего черновика, иначе первый
активный), чтобы не упереться в проверку владельца черновика в save_reading.
"""
from __future__ import annotations

import secrets
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.utility.models import Room, SupportTicket, User, MeterReading

# Маркер переписки QR-портала (фильтр /messages + авто-чистка 5 дней в
# cleanup_qr_tickets_task). Живёт здесь, а не в роутере: нужен и сервисам
# (уведомление об отклонении), без циклического импорта роутера.
QR_TICKET_SUBJECT = "Обращение с QR-портала"


def generate_qr_token() -> str:
    """32 байта энтропии → ~43 url-safe символа. Неугадываемо, неподделываемо."""
    return secrets.token_urlsafe(32)


async def get_or_create_room_token(db: AsyncSession, room: Room) -> str:
    """Лениво выдаёт токен комнаты (при первой генерации QR в админке)."""
    if not room.qr_token:
        room.qr_token = generate_qr_token()
        db.add(room)
        await db.commit()
    return room.qr_token


async def regenerate_room_token(db: AsyncSession, room: Room) -> str:
    """Отзыв = перегенерация: старый QR перестаёт резолвиться (404).
    Пароль портала сбрасываем тоже: перевыпуск обычно = компрометация,
    и старый пароль мог утечь вместе с QR."""
    room.qr_token = generate_qr_token()
    room.qr_password_hash = None
    db.add(room)
    await db.commit()
    return room.qr_token


async def resolve_room_by_token(db: AsyncSession, token: Optional[str]) -> Optional[Room]:
    """Комната по токену. Короткие/пустые токены отсекаем сразу (не лезем в БД)."""
    if not token or len(token) < 16:
        return None
    return (await db.execute(
        select(Room).where(Room.qr_token == token)
    )).scalars().first()


async def pick_representative_user_id(
    db: AsyncSession, room_id: int, period_id: Optional[int]
) -> Optional[int]:
    """Чей лицевой счёт ведёт подачу по QR.

    1) Владелец текущего черновика этого периода — чтобы повторная подача
       (правка) шла в тот же черновик, без «уже передано другим жильцом».
    2) Иначе — первый активный жилец комнаты (детерминированно, по min id).
    None → в комнате нет жильцов (вакантная) → подача невозможна.
    """
    if period_id:
        draft_owner = (await db.execute(
            select(MeterReading.user_id).where(
                MeterReading.room_id == room_id,
                MeterReading.period_id == period_id,
                MeterReading.is_approved.is_(False),
                MeterReading.user_id.is_not(None),
            ).limit(1)
        )).scalars().first()
        if draft_owner:
            return draft_owner

    return (await db.execute(
        select(User.id).where(
            User.room_id == room_id,
            User.is_deleted.is_(False),
            User.role == "user",
        ).order_by(User.id).limit(1)
    )).scalars().first()


def notify_reading_rejected(
    db: AsyncSession,
    user_id: int,
    period_name: Optional[str] = None,
    reason: Optional[str] = None,
) -> None:
    """Системное уведомление жильцу в переписку QR-портала: показание отклонено.

    Канал — те же SupportTicket с QR-темой: портал уже умеет их показывать,
    авто-чистка через 5 дней уже есть. status=answered сразу (это не вопрос
    жильца, отвечать не на что). Без commit — коммитит вызывающий.
    """
    text = "❌ Ваши показания" + (f" за {period_name}" if period_name else "") + \
        " отклонены администратором."
    if reason:
        text += f" Причина: {reason.strip()}"
    text += " Пожалуйста, проверьте цифры на счётчиках и подайте показания заново."
    db.add(SupportTicket(
        user_id=user_id,
        subject=QR_TICKET_SUBJECT,
        message="Проверка поданных показаний",
        status="answered",
        admin_response=text,
    ))
