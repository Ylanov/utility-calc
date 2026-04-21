from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, status, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.core.database import get_arsenal_db
from app.modules.arsenal.models import ArsenalUser
from app.core.auth import verify_password, create_access_token
from app.core.config import settings

router = APIRouter(tags=["Arsenal Auth"])

# Защита от перебора пароля: блокировка после N неудачных попыток.
# Значения sane-defaults, можно вынести в settings если нужно.
LOCKOUT_THRESHOLD = 5         # после 5 промахов блокируем
LOCKOUT_MINUTES = 15          # на 15 минут


@router.post("/api/arsenal/login")
async def arsenal_login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_arsenal_db)
):
    result = await db.execute(select(ArsenalUser).where(ArsenalUser.username == form_data.username))
    user = result.scalars().first()

    now = datetime.utcnow()

    # Ошибки авторизации: общее сообщение без раскрытия «пользователь есть / нет»,
    # но отдельные 403 для отключённой учётки и залоченной (после перебора) —
    # это помогает админу понять ситуацию быстрее.
    if user and user.locked_until and user.locked_until > now:
        remaining = int((user.locked_until - now).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Учётка временно заблокирована (ещё {remaining} мин). Попробуйте позже или обратитесь к администратору.",
        )
    if user and not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Учётная запись отключена. Обратитесь к администратору.",
        )

    if not user or not verify_password(form_data.password, user.hashed_password):
        # Промах: инкрементируем счётчик, при превышении порога — блокируем.
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= LOCKOUT_THRESHOLD:
                user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
                user.failed_login_count = 0  # обнуляем — lock сам себя стерёжет
            await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные Арсенала",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Успех: сбрасываем счётчики, пишем last_login_*
    from app.modules.arsenal.services.audit import (
        client_ip_from_request, write_arsenal_audit,
    )
    ip = client_ip_from_request(request)
    user.last_login_at = now
    user.last_login_ip = ip
    user.failed_login_count = 0
    user.locked_until = None

    await write_arsenal_audit(
        db, user_id=user.id, username=user.username,
        action="login", entity_type="user", entity_id=user.id,
        details={"role": user.role}, ip_address=ip,
    )
    await db.commit()

    access_token = create_access_token(
        data={
            "sub": user.username,
            "scope": "arsenal_admin"
        }
    )

    # ИСПРАВЛЕНИЕ ЗДЕСЬ: Убрали 'Bearer ' из value
    response.set_cookie(
        key="access_token",
        value=access_token, # <-- Только токен
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
        secure=settings.ENVIRONMENT == "production"
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": user.role,  # <--- ДОБАВИТЬ ЭТУ СТРОКУ
        "redirect_url": "arsenal_dashboard.html"
    }

@router.post("/api/arsenal/logout")
async def arsenal_logout(response: Response):
    response.delete_cookie(
        key="access_token",
        samesite="lax",
        httponly=True,
        secure=settings.ENVIRONMENT == "production"
    )
    return {"status": "success"}
