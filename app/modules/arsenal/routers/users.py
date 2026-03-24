import string
import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.database import get_arsenal_db
from app.modules.arsenal.models import ArsenalUser
from app.modules.arsenal.deps import get_current_arsenal_user, pwd_context

router = APIRouter(tags=["Arsenal Users"])

@router.get("/users")
async def get_users(
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    stmt = select(ArsenalUser).options(selectinload(ArsenalUser.accounting_object)).order_by(ArsenalUser.id)
    result = await db.execute(stmt)
    users = result.scalars().all()

    response = []
    for u in users:
        response.append({
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "object_name": u.accounting_object.name if u.accounting_object else "Главное управление",
            "created_at": u.created_at.strftime("%d.%m.%Y")
        })
    return response

@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
        user_id: int,
        db: AsyncSession = Depends(get_arsenal_db),
        current_user: ArsenalUser = Depends(get_current_arsenal_user)
):
    if current_user.role != "admin":
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    user = await db.get(ArsenalUser, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    alphabet = string.ascii_letters + string.digits
    new_password = ''.join(secrets.choice(alphabet) for _ in range(8))

    user.hashed_password = pwd_context.hash(new_password)
    db.add(user)
    await db.commit()

    return {
        "message": "Пароль успешно сброшен",
        "username": user.username,
        "new_password": new_password
    }
