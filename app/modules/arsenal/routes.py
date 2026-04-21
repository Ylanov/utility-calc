from fastapi import APIRouter

# Импортируем все наши разделенные роутеры
from app.modules.arsenal.routers import (
    objects, nomenclature, documents, users, system, ops,
)

# Создаем главный роутер для модуля Арсенал
router = APIRouter(prefix="/api/arsenal")

# Подключаем к нему все sub-routers
router.include_router(objects.router)
router.include_router(nomenclature.router)
router.include_router(documents.router)
router.include_router(users.router)
router.include_router(system.router)
# НОВОЕ: rollback, disposal-reasons, low-stock alerts, inventory, password reset
router.include_router(ops.router)
