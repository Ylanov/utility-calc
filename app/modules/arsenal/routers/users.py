"""Arsenal users — полноценное управление учётками админом.

Endpoints:
    GET    /users                   — список с фильтрами + статистика
    POST   /users                   — создание + автогенерация пароля/reset-ссылка
    GET    /users/{id}              — карточка с детальной статистикой
    PATCH  /users/{id}              — изменение role / object / full_name / email / phone / is_active
    POST   /users/{id}/deactivate   — отключить (с reason) вместо удаления
    POST   /users/{id}/activate     — вернуть отключённого в строй
    POST   /users/{id}/unlock       — снять lockout после перебора пароля
    DELETE /users/{id}              — жёсткое удаление (только если нет связанных документов)
    GET    /users/{id}/activity     — проведённые документы + аудит-события пользователя

Старый /users/{id}/reset-password (plaintext) — deprecated, редиректим на
ops.create_password_reset_link (через токен).
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import get_arsenal_db
from app.core.auth import get_password_hash
from app.modules.arsenal.deps import get_current_arsenal_user
from app.modules.arsenal.models import (
    AccountingObject,
    ArsenalAuditLog,
    ArsenalUser,
    Document,
)
from app.modules.arsenal.services.audit import client_ip_from_request, write_arsenal_audit

router = APIRouter(tags=["Arsenal Users"])


def _require_admin(user: ArsenalUser) -> None:
    if user.role != "admin":
        raise HTTPException(403, "Только для администратора")


def _gen_password() -> str:
    """Генерация пароля — 12 символов без визуально похожих (0/O, 1/l/I)."""
    alphabet = [c for c in (string.ascii_letters + string.digits) if c not in "0O1lI"]
    return "".join(secrets.choice(alphabet) for _ in range(12))


# =====================================================================
# LIST
# =====================================================================
@router.get("/users")
async def list_users(
    q: Optional[str] = Query(None, description="Поиск по username / full_name / email"),
    role: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    object_id: Optional[int] = Query(None),
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    _require_admin(current_user)

    stmt = select(ArsenalUser).options(
        selectinload(ArsenalUser.accounting_object),
    ).order_by(ArsenalUser.id)

    conds = []
    if q:
        pattern = f"%{q.strip()}%"
        conds.append(
            (ArsenalUser.username.ilike(pattern))
            | (ArsenalUser.full_name.ilike(pattern))
            | (ArsenalUser.email.ilike(pattern))
        )
    if role:
        conds.append(ArsenalUser.role == role)
    if is_active is not None:
        conds.append(ArsenalUser.is_active.is_(is_active))
    if object_id:
        conds.append(ArsenalUser.object_id == object_id)
    if conds:
        stmt = stmt.where(and_(*conds))

    users = (await db.execute(stmt)).scalars().all()

    # Подтянем счётчик проведённых документов одним запросом
    user_ids = [u.id for u in users]
    docs_count_by_user: dict[int, int] = {}
    if user_ids:
        rows = (await db.execute(
            select(Document.author_id, func.count(Document.id))
            .where(Document.author_id.in_(user_ids))
            .group_by(Document.author_id)
        )).all()
        docs_count_by_user = {uid: int(cnt) for uid, cnt in rows}

    now = datetime.utcnow()
    response = []
    for u in users:
        locked = bool(u.locked_until and u.locked_until > now)
        response.append({
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "role": u.role,
            "object_id": u.object_id,
            "object_name": u.accounting_object.name if u.accounting_object else "Главное управление",
            "is_active": bool(u.is_active),
            "is_locked": locked,
            "locked_until": u.locked_until.isoformat() if u.locked_until else None,
            "failed_login_count": u.failed_login_count or 0,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "last_login_ip": u.last_login_ip,
            "created_at": u.created_at.strftime("%d.%m.%Y") if u.created_at else None,
            "documents_count": docs_count_by_user.get(u.id, 0),
            "deactivated_at": u.deactivated_at.isoformat() if u.deactivated_at else None,
            "deactivation_reason": u.deactivation_reason,
        })
    return response


# =====================================================================
# DETAIL
# =====================================================================
@router.get("/users/{user_id}")
async def user_detail(
    user_id: int,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    _require_admin(current_user)
    u = (await db.execute(
        select(ArsenalUser)
        .options(selectinload(ArsenalUser.accounting_object))
        .where(ArsenalUser.id == user_id)
    )).scalars().first()
    if not u:
        raise HTTPException(404, "Пользователь не найден")

    # Статистика документов (по типам)
    docs_by_type = (await db.execute(
        select(Document.operation_type, func.count(Document.id))
        .where(Document.author_id == user_id)
        .group_by(Document.operation_type)
    )).all()

    # Последние 10 документов
    recent_docs = (await db.execute(
        select(Document)
        .where(Document.author_id == user_id)
        .order_by(Document.created_at.desc())
        .limit(10)
    )).scalars().all()

    # Последние 20 audit-записей
    recent_audit = (await db.execute(
        select(ArsenalAuditLog)
        .where(ArsenalAuditLog.user_id == user_id)
        .order_by(ArsenalAuditLog.created_at.desc())
        .limit(20)
    )).scalars().all()

    return {
        "user": {
            "id": u.id, "username": u.username, "full_name": u.full_name,
            "email": u.email, "phone": u.phone,
            "role": u.role, "object_id": u.object_id,
            "object_name": u.accounting_object.name if u.accounting_object else None,
            "is_active": bool(u.is_active),
            "locked_until": u.locked_until.isoformat() if u.locked_until else None,
            "failed_login_count": u.failed_login_count or 0,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "last_login_ip": u.last_login_ip,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "deactivated_at": u.deactivated_at.isoformat() if u.deactivated_at else None,
            "deactivation_reason": u.deactivation_reason,
        },
        "stats": {
            "documents_total": sum(int(c) for _, c in docs_by_type),
            "documents_by_type": {op: int(c) for op, c in docs_by_type},
        },
        "recent_documents": [
            {
                "id": d.id, "doc_number": d.doc_number,
                "operation_type": d.operation_type,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "is_reversed": bool(d.is_reversed),
            }
            for d in recent_docs
        ],
        "recent_audit": [
            {
                "id": a.id, "action": a.action, "entity_type": a.entity_type,
                "entity_id": a.entity_id, "details": a.details,
                "ip_address": a.ip_address,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in recent_audit
        ],
    }


# =====================================================================
# CREATE
# =====================================================================
class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    role: str = Field(..., pattern="^(admin|unit_head)$")
    object_id: Optional[int] = None
    full_name: Optional[str] = None
    email: Optional[str] = None   # Нестрогий — просто строка; email-validation через pydantic опциональна
    phone: Optional[str] = None
    password: Optional[str] = Field(None, min_length=8, max_length=128,
                                    description="Если пусто — сгенерируется и вернётся одноразовой ссылкой")


@router.post("/users")
async def create_user(
    data: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    _require_admin(current_user)

    # unit_head без object_id — бессмысленно: он не увидит никаких документов/остатков
    if data.role == "unit_head" and not data.object_id:
        raise HTTPException(400, "Для unit_head обязательно привязать object_id (склад)")

    exists = (await db.execute(
        select(ArsenalUser).where(ArsenalUser.username == data.username)
    )).scalars().first()
    if exists:
        raise HTTPException(409, f"Пользователь «{data.username}» уже существует")

    if data.object_id:
        obj = await db.get(AccountingObject, data.object_id)
        if not obj:
            raise HTTPException(400, "Объект не найден")

    # Пароль: если передан — используем как есть; если нет — генерируем и
    # возвращаем в ответе (для одноразовой передачи). Позже пользователь
    # может сбросить через /reset-password-link.
    generated = None
    plain = data.password
    if not plain:
        plain = _gen_password()
        generated = plain

    new_user = ArsenalUser(
        username=data.username,
        hashed_password=get_password_hash(plain),
        role=data.role,
        object_id=data.object_id,
        full_name=data.full_name,
        email=data.email,
        phone=data.phone,
        is_active=True,
    )
    db.add(new_user)
    await db.flush()

    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="create_user", entity_type="user", entity_id=new_user.id,
        details={
            "new_username": new_user.username, "role": new_user.role,
            "object_id": new_user.object_id,
        },
        ip_address=client_ip_from_request(request),
    )
    await db.commit()
    await db.refresh(new_user)

    return {
        "id": new_user.id,
        "username": new_user.username,
        "role": new_user.role,
        "object_id": new_user.object_id,
        "generated_password": generated,  # None если админ задал свой
    }


# =====================================================================
# UPDATE
# =====================================================================
class UserPatch(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    role: Optional[str] = Field(None, pattern="^(admin|unit_head)$")
    object_id: Optional[int] = None
    is_active: Optional[bool] = None


@router.patch("/users/{user_id}")
async def update_user(
    user_id: int,
    data: UserPatch,
    request: Request,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    _require_admin(current_user)
    u = await db.get(ArsenalUser, user_id)
    if not u:
        raise HTTPException(404, "Пользователь не найден")

    # Не даём админу случайно понизить самого себя до unit_head
    # (нельзя остаться без админов в системе).
    if user_id == current_user.id and data.role == "unit_head":
        raise HTTPException(400, "Нельзя понизить собственную учётку с роли admin")

    if data.object_id is not None:
        obj = await db.get(AccountingObject, data.object_id)
        if not obj:
            raise HTTPException(400, "Объект не найден")

    changes: dict = {}
    for field in ("full_name", "email", "phone", "role", "object_id"):
        new_val = getattr(data, field)
        if new_val is not None and new_val != getattr(u, field):
            changes[field] = {"old": getattr(u, field), "new": new_val}
            setattr(u, field, new_val)

    if data.is_active is not None and data.is_active != u.is_active:
        changes["is_active"] = {"old": u.is_active, "new": data.is_active}
        u.is_active = data.is_active
        if data.is_active:
            # Реактивация: чистим метки отключения
            u.deactivated_at = None
            u.deactivated_by_id = None
            u.deactivation_reason = None
        else:
            u.deactivated_at = datetime.utcnow()
            u.deactivated_by_id = current_user.id

    if not changes:
        return {"status": "noop"}

    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="update_user", entity_type="user", entity_id=user_id,
        details={"target": u.username, "changes": changes},
        ip_address=client_ip_from_request(request),
    )
    await db.commit()
    return {"status": "ok", "changes": changes}


# =====================================================================
# DEACTIVATE / ACTIVATE / UNLOCK (отдельные явные кнопки для UI)
# =====================================================================
class DeactivateBody(BaseModel):
    reason: Optional[str] = None


@router.post("/users/{user_id}/deactivate")
async def deactivate_user(
    user_id: int,
    body: Optional[DeactivateBody] = None,
    request: Request = None,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    _require_admin(current_user)
    if user_id == current_user.id:
        raise HTTPException(400, "Нельзя отключить собственную учётку")
    u = await db.get(ArsenalUser, user_id)
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    if not u.is_active:
        return {"status": "noop"}

    u.is_active = False
    u.deactivated_at = datetime.utcnow()
    u.deactivated_by_id = current_user.id
    u.deactivation_reason = (body.reason if body else None)

    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="deactivate_user", entity_type="user", entity_id=user_id,
        details={"target": u.username, "reason": u.deactivation_reason},
        ip_address=client_ip_from_request(request) if request else None,
    )
    await db.commit()
    return {"status": "deactivated"}


@router.post("/users/{user_id}/activate")
async def activate_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    _require_admin(current_user)
    u = await db.get(ArsenalUser, user_id)
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    if u.is_active:
        return {"status": "noop"}
    u.is_active = True
    u.deactivated_at = None
    u.deactivated_by_id = None
    u.deactivation_reason = None

    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="activate_user", entity_type="user", entity_id=user_id,
        details={"target": u.username},
        ip_address=client_ip_from_request(request),
    )
    await db.commit()
    return {"status": "activated"}


@router.post("/users/{user_id}/unlock")
async def unlock_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    """Снять временную блокировку после превышения неудачных попыток."""
    _require_admin(current_user)
    u = await db.get(ArsenalUser, user_id)
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    u.locked_until = None
    u.failed_login_count = 0

    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="unlock_user", entity_type="user", entity_id=user_id,
        details={"target": u.username},
        ip_address=client_ip_from_request(request),
    )
    await db.commit()
    return {"status": "unlocked"}


# =====================================================================
# DELETE (hard, с проверкой целостности)
# =====================================================================
@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    """Жёсткое удаление. Разрешено только если у пользователя НЕТ связанных
    документов (author_id) — иначе audit-trail сломается. Для активных
    пользователей админ должен использовать deactivate."""
    _require_admin(current_user)
    if user_id == current_user.id:
        raise HTTPException(400, "Нельзя удалить собственную учётку")
    u = await db.get(ArsenalUser, user_id)
    if not u:
        raise HTTPException(404, "Пользователь не найден")

    doc_cnt = (await db.execute(
        select(func.count(Document.id)).where(Document.author_id == user_id)
    )).scalar_one()
    if doc_cnt:
        raise HTTPException(
            409,
            f"У пользователя есть {doc_cnt} проведённых документов. "
            "Используйте «Отключить» вместо удаления — это сохранит историю.",
        )

    await write_arsenal_audit(
        db, user_id=current_user.id, username=current_user.username,
        action="delete_user", entity_type="user", entity_id=user_id,
        details={"target": u.username},
        ip_address=client_ip_from_request(request),
    )
    await db.delete(u)
    await db.commit()
    return {"status": "deleted"}


# =====================================================================
# ACTIVITY — отдельный endpoint для «таймлайна» пользователя
# =====================================================================
@router.get("/users/{user_id}/activity")
async def user_activity(
    user_id: int,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_arsenal_db),
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    _require_admin(current_user)
    rows = (await db.execute(
        select(ArsenalAuditLog)
        .where(ArsenalAuditLog.user_id == user_id)
        .order_by(ArsenalAuditLog.created_at.desc())
        .limit(limit)
    )).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "action": r.action,
                "entity_type": r.entity_type, "entity_id": r.entity_id,
                "details": r.details, "ip_address": r.ip_address,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


# =====================================================================
# /me — для определения прав на фронте
# =====================================================================
@router.get("/me")
async def get_current_user_info(
    current_user: ArsenalUser = Depends(get_current_arsenal_user),
):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "full_name": current_user.full_name,
        "role": current_user.role,
        "object_id": current_user.object_id,
        "is_active": bool(current_user.is_active),
    }


# =====================================================================
# DEPRECATED: /users/{id}/reset-password — удаляем plaintext ответ
# =====================================================================
@router.post("/users/{user_id}/reset-password", deprecated=True)
async def reset_user_password_deprecated(user_id: int):
    raise HTTPException(
        status_code=410,
        detail=(
            "Этот endpoint устарел (возвращал пароль в plaintext). "
            "Используйте POST /api/arsenal/users/{id}/reset-password-link — "
            "он создаёт одноразовую безопасную ссылку для установки пароля пользователем."
        ),
    )
