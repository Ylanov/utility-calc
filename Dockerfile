# ================================
# ЭТАП 1: BUILDER
# Устанавливает зависимости для сборки Python-пакетов
# ================================
FROM python:3.12-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Системные зависимости, нужные для компиляции (например, psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    libcairo2-dev \
    libpango1.0-dev \
    libgdk-pixbuf2.0-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Используем uv для быстрой установки
RUN pip install --upgrade pip uv

COPY requirements.txt .

# Устанавливаем пакеты в системный Python, чтобы потом скопировать всё окружение
RUN uv pip install --system -r requirements.txt


# ================================
# ЭТАП 2: BASE
# Базовый образ с установленными Python-пакетами и runtime-зависимостями
# ================================
FROM python:3.12-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Системные зависимости, нужные для работы приложения (например, для WeasyPrint)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libffi8 \
    libcairo2 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    fonts-liberation \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Копируем всё окружение Python (включая пакеты) из builder'а
COPY --from=builder /usr/local /usr/local

# Создаем пользователя без прав root для безопасности
RUN useradd -ms /bin/bash appuser
USER appuser


# ================================
# ЭТАП 3: WEB (API)
# Финальный образ для веб-сервера
# ================================
FROM base AS web

WORKDIR /app

# Копируем только код, необходимый для работы API
COPY --chown=appuser:appuser app app
COPY --chown=appuser:appuser templates templates
COPY --chown=appuser:appuser static static
COPY --chown=appuser:appuser alembic alembic
COPY --chown=appuser:appuser main.py .

EXPOSE 8000

# Запускаем приложение через Gunicorn + Uvicorn для продакшена
CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000"]


# ================================
# ЭТАП 4: WORKER (Celery)
# Финальный образ для фоновых задач
# ================================
FROM base AS worker

WORKDIR /app

# Копируем код, необходимый для воркера
COPY --chown=appuser:appuser app app
COPY --chown=appuser:appuser templates templates
COPY --chown=appuser:appuser worker.py .
COPY --chown=appuser:appuser tasks.py .

# Запускаем Celery-воркер
CMD ["celery", "-A", "app.worker", "worker", "--loglevel=info", "--concurrency=4", "--prefetch-multiplier=1"]