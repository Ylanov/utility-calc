from fastapi import APIRouter, Depends, HTTPException, status, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.core.database import get_gsm_db
from app.modules.gsm.models import GsmUser
from app.core.auth import verify_password, create_access_token
from app.core.config import settings

router = APIRouter(tags=["GSM Auth"])

@router.post("/api/gsm/login")
async def gsm_login(
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_gsm_db)
):
    # Ищем пользователя в таблице gsm_users
    result = await db.execute(select(GsmUser).where(GsmUser.username == form_data.username))
    user = result.scalars().first()

    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные ГСМ",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Генерируем токен
    access_token = create_access_token(
        data={
            "sub": user.username,
            "scope": "gsm_admin"
        }
    )

    # Сохраняем токен в защищенной куке
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
        secure=settings.ENVIRONMENT == "production"
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "role": user.role,  # Возвращаем роль для фронтенда (admin / storage_head)
        "redirect_url": "gsm_dashboard.html"
    }

@router.post("/api/gsm/logout")
async def gsm_logout(response: Response):
    response.delete_cookie(
        key="access_token",
        samesite="lax",
        httponly=True,
        secure=settings.ENVIRONMENT == "production"
    )
    return {"status": "success"}