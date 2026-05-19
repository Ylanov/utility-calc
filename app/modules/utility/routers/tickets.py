"""Обращения жильцов (support tickets) — backend.

Endpoints:
  - POST   /api/me/tickets               — жилец создаёт обращение
  - GET    /api/me/tickets               — жилец смотрит свои
  - GET    /api/me/tickets/{id}          — деталь одного
  - GET    /api/admin/tickets            — админ видит все, с фильтрами
  - PATCH  /api/admin/tickets/{id}       — админ отвечает / меняет статус

Жильцовские эндпоинты — за require_resident (только role=user).
Админские — за allow_management (admin/accountant/financier).
"""
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import RoleChecker, require_resident
from app.core.time_utils import utcnow
from app.modules.utility.models import SupportTicket, User
from app.modules.utility.routers.admin_dashboard import write_audit_log


allow_management = RoleChecker(["accountant", "admin", "financier"])

router_client = APIRouter(prefix="/api/me/tickets", tags=["Client — Support tickets"])
router_admin = APIRouter(prefix="/api/admin/tickets", tags=["Admin — Support tickets"])


# =========================================================================
# SCHEMAS
# =========================================================================
class TicketCreateBody(BaseModel):
    subject: str = Field(..., min_length=3, max_length=200)
    message: str = Field(..., min_length=10, max_length=5000)


class TicketAdminUpdateBody(BaseModel):
    admin_response: Optional[str] = Field(None, max_length=5000)
    # Допустимые переходы: open → in_progress → answered → closed.
    # Сервер не валидирует строго: админ может вернуть в open если ошибся.
    status: Optional[str] = Field(None, pattern="^(open|in_progress|answered|closed)$")


class TicketOut(BaseModel):
    id: int
    subject: str
    message: str
    status: str
    admin_response: Optional[str] = None
    responded_by_username: Optional[str] = None
    responded_at: Optional[datetime] = None
    created_at: datetime
    # Для админ-списка показываем кто задал.
    user_id: int
    username: Optional[str] = None

    class Config:
        from_attributes = True


class TicketListResponse(BaseModel):
    total: int
    items: List[TicketOut]


def _to_out(t: SupportTicket, user_username: Optional[str] = None, responded_by_username: Optional[str] = None) -> dict:
    return {
        "id": t.id,
        "subject": t.subject,
        "message": t.message,
        "status": t.status,
        "admin_response": t.admin_response,
        "responded_by_username": responded_by_username,
        "responded_at": t.responded_at,
        "created_at": t.created_at,
        "user_id": t.user_id,
        "username": user_username,
    }


# =========================================================================
# CLIENT — жилец
# =========================================================================
@router_client.post("", response_model=TicketOut)
async def create_ticket(
    body: TicketCreateBody,
    current_user: User = Depends(require_resident),
    db: AsyncSession = Depends(get_db),
):
    """Жилец создаёт новое обращение."""
    ticket = SupportTicket(
        user_id=current_user.id,
        subject=body.subject.strip(),
        message=body.message.strip(),
        status="open",
    )
    db.add(ticket)
    await db.flush()
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="create", entity_type="ticket", entity_id=ticket.id,
        details={"subject": ticket.subject[:60]},
    )
    await db.commit()
    await db.refresh(ticket)
    return _to_out(ticket, user_username=current_user.username)


@router_client.get("", response_model=TicketListResponse)
async def list_my_tickets(
    current_user: User = Depends(require_resident),
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    """Жилец смотрит ТОЛЬКО свои обращения (свежие сверху)."""
    base = select(SupportTicket).where(SupportTicket.user_id == current_user.id)
    total = (await db.execute(
        select(func.count(SupportTicket.id)).where(SupportTicket.user_id == current_user.id)
    )).scalar_one()

    rows = (await db.execute(
        base.options(selectinload(SupportTicket.responded_by))
        .order_by(desc(SupportTicket.created_at))
        .offset((page - 1) * limit).limit(limit)
    )).scalars().all()

    items = [
        _to_out(
            t,
            user_username=current_user.username,
            responded_by_username=t.responded_by.username if t.responded_by else None,
        )
        for t in rows
    ]
    return {"total": total, "items": items}


@router_client.get("/{ticket_id}", response_model=TicketOut)
async def get_my_ticket(
    ticket_id: int,
    current_user: User = Depends(require_resident),
    db: AsyncSession = Depends(get_db),
):
    """Деталь одного обращения — только если оно принадлежит этому жильцу."""
    t = (await db.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.responded_by))
        .where(SupportTicket.id == ticket_id, SupportTicket.user_id == current_user.id)
    )).scalars().first()
    if not t:
        raise HTTPException(404, "Обращение не найдено")
    return _to_out(
        t,
        user_username=current_user.username,
        responded_by_username=t.responded_by.username if t.responded_by else None,
    )


# =========================================================================
# ADMIN
# =========================================================================
@router_admin.get("", response_model=TicketListResponse)
async def list_all_tickets(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(allow_management),
    status: Optional[str] = Query(None, pattern="^(open|in_progress|answered|closed)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    """Все обращения — для админа. Свежие открытые сверху."""
    base = select(SupportTicket).options(
        selectinload(SupportTicket.user),
        selectinload(SupportTicket.responded_by),
    )
    count_q = select(func.count(SupportTicket.id))
    if status:
        base = base.where(SupportTicket.status == status)
        count_q = count_q.where(SupportTicket.status == status)

    total = (await db.execute(count_q)).scalar_one()
    rows = (await db.execute(
        base.order_by(
            # Сначала open и in_progress (по приоритету), затем по дате DESC.
            (SupportTicket.status == "open").desc(),
            (SupportTicket.status == "in_progress").desc(),
            desc(SupportTicket.created_at),
        )
        .offset((page - 1) * limit).limit(limit)
    )).scalars().all()

    items = [
        _to_out(
            t,
            user_username=t.user.username if t.user else None,
            responded_by_username=t.responded_by.username if t.responded_by else None,
        )
        for t in rows
    ]
    return {"total": total, "items": items}


@router_admin.patch("/{ticket_id}", response_model=TicketOut)
async def respond_to_ticket(
    ticket_id: int,
    body: TicketAdminUpdateBody,
    current_user: User = Depends(allow_management),
    db: AsyncSession = Depends(get_db),
):
    """Админ отвечает на обращение и/или меняет статус.

    Если передан admin_response — фиксируем кто ответил и когда, а статус
    переходит в `answered` (если админ явно не задал другой).
    """
    t = (await db.execute(
        select(SupportTicket)
        .options(selectinload(SupportTicket.user))
        .where(SupportTicket.id == ticket_id)
    )).scalars().first()
    if not t:
        raise HTTPException(404, "Обращение не найдено")

    changed = []
    if body.admin_response is not None and body.admin_response.strip():
        t.admin_response = body.admin_response.strip()
        t.responded_by_id = current_user.id
        t.responded_at = utcnow()
        if not body.status:
            t.status = "answered"
        changed.append("response")
    if body.status:
        t.status = body.status
        changed.append(f"status={body.status}")

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="respond", entity_type="ticket", entity_id=t.id,
        details={"changes": changed, "ticket_user": t.user.username if t.user else None},
    )
    await db.commit()
    await db.refresh(t)
    return _to_out(
        t,
        user_username=t.user.username if t.user else None,
        responded_by_username=current_user.username if t.responded_by_id else None,
    )
