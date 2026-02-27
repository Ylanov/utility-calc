# ==========================================
# ===== BUILDER STAGE (Сборка пакетов) =====
# ==========================================
FROM python:3.13-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Устанавливаем системные зависимости для сборки (WeasyPrint и БД)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    libcairo2-dev \
    libpango1.0-dev \
    libgdk-pixbuf2.0-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем UV (быстрый установщик пакетов)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Устанавливаем зависимости Python
COPY requirements.txt .
RUN uv pip install --system -r requirements.txt

# ==========================================
# ===== FINAL STAGE (Финальный образ) ======
# ==========================================
FROM python:3.13-slim-bookworm AS app_runner

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Устанавливаем runtime-зависимости
RUN apt-get update && apt-get install -y --no-install-recommends --fix-missing \
    libpq5 \
    libffi8 \
    libcairo2 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Копируем установленные пакеты из builder
COPY --from=builder /usr/local /usr/local

# Создаем пользователя
RUN useradd -ms /bin/bash appuser && \
    mkdir -p /app/static/generated_files && \
    chown -R appuser:appuser /app

# Копируем код и конфигурации
# Важно: app/ теперь содержит modules/ и core/
COPY --chown=appuser:appuser app/ app/
COPY --chown=appuser:appuser templates/ templates/
COPY --chown=appuser:appuser static/ static/

# Копируем миграции ЖКХ
COPY --chown=appuser:appuser alembic/ alembic/
COPY --chown=appuser:appuser alembic.ini .

# Копируем миграции Арсенала/ГСМ
COPY --chown=appuser:appuser alembic_arsenal/ alembic_arsenal/
COPY --chown=appuser:appuser alembic_arsenal.ini .

USER appuser

EXPOSE 8000
# CMD задается в docker-compose