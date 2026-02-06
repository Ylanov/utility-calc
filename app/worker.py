import os
from celery import Celery
from app.config import settings

# Создаем приложение Celery
celery = Celery(
    "app",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL
)

# Настройки Celery
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# Указываем, где искать задачи (Tasks)
celery.conf.imports = [
    "app.tasks"
]