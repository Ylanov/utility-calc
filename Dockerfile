# ================================
# BUILDER
# ================================
FROM python:3.12-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Зависимости для сборки
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    libcairo2-dev \
    libpango1.0-dev \
    libgdk-pixbuf2.0-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем uv
RUN pip install --upgrade pip setuptools wheel uv

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем зависимости максимально быстро
RUN uv pip install --system -r requirements.txt


# ================================
# FINAL BASE
# ================================
FROM python:3.12-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# Только runtime-зависимости
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

# Копируем Python + пакеты
COPY --from=builder /usr/local /usr/local

# Безопасный пользователь
RUN useradd -m appuser
USER appuser


# ================================
# WEB
# ================================
FROM base AS web

WORKDIR /app

COPY app app
COPY templates templates
COPY static static
COPY alembic alembic

EXPOSE 8000

CMD ["gunicorn", "app.main:app", "-k", "uvicorn.workers.UvicornWorker", "-w", "4", "-b", "0.0.0.0:8000"]


# ================================
# WORKER
# ================================
FROM base AS worker

WORKDIR /app

COPY app app
COPY templates templates

CMD ["celery", "-A", "app.worker.celery", "worker", "--loglevel=info", "--concurrency=4", "--prefetch-multiplier=1"]
