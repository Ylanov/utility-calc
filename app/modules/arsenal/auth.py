from fastapi import APIRouter, Depends, HTTPException, status, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.core.database import get_arsenal_db
from app.modules.arsenal.models import ArsenalUser
from app.core.auth import verify_password, create_access_token
from app.core.config import settings

router = APIRouter(tags=["Arsenal Auth"])

@router.post("/api/arsenal/login")
async def arsenal_login(
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_arsenal_db)
):
    result = await db.execute(select(ArsenalUser).where(ArsenalUser.username == form_data.username))
    user = result.scalars().first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные Арсенала",
            headers={"WWW-Authenticate": "Bearer"},
        )

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