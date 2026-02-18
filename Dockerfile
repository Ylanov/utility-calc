# =========================
# ===== BUILDER STAGE =====
# =========================
FROM python:3.12-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    libcairo2-dev \
    libpango1.0-dev \
    libgdk-pixbuf2.0-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip uv

COPY requirements.txt .
RUN uv pip install --system -r requirements.txt


# =========================
# ===== BASE STAGE ========
# =========================
FROM python:3.12-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive
WORKDIR /app

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

COPY --from=builder /usr/local /usr/local

RUN useradd -ms /bin/bash appuser
RUN mkdir -p /app/static/generated_files && \
    chown -R appuser:appuser /app

USER appuser


# =========================
# ===== WEB STAGE =========
# =========================
FROM base AS web

WORKDIR /app

# Application files
COPY --chown=appuser:appuser app app
COPY --chown=appuser:appuser templates templates
COPY --chown=appuser:appuser static static

# Utility DB alembic
COPY --chown=appuser:appuser alembic alembic
COPY --chown=appuser:appuser alembic.ini .

# Arsenal DB alembic
COPY --chown=appuser:appuser alembic_arsenal alembic_arsenal
COPY --chown=appuser:appuser alembic_arsenal.ini .

EXPOSE 8000

CMD ["sh", "-c", "\
echo 'Running Utility DB migrations...' && \
alembic upgrade head && \
echo 'Running Arsenal DB migrations...' && \
alembic -c alembic_arsenal.ini upgrade head && \
echo 'Starting Gunicorn...' && \
gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000 \
"]


# =========================
# ===== WORKER STAGE ======
# =========================
FROM base AS worker

WORKDIR /app

COPY --chown=appuser:appuser app app
COPY --chown=appuser:appuser templates templates

# (миграции worker не запускает, но конфиги оставим на случай future use)
COPY --chown=appuser:appuser alembic alembic
COPY --chown=appuser:appuser alembic.ini .
COPY --chown=appuser:appuser alembic_arsenal alembic_arsenal
COPY --chown=appuser:appuser alembic_arsenal.ini .

CMD ["celery", "-A", "app.worker", "worker", "--loglevel=info"]
