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


FROM base AS web

WORKDIR /app

COPY --chown=appuser:appuser app app
COPY --chown=appuser:appuser templates templates
COPY --chown=appuser:appuser static static
COPY --chown=appuser:appuser alembic alembic
COPY --chown=appuser:appuser alembic.ini .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000"]




FROM base AS worker

WORKDIR /app

COPY --chown=appuser:appuser app app
COPY --chown=appuser:appuser templates templates
COPY --chown=appuser:appuser alembic alembic
COPY --chown=appuser:appuser alembic.ini .

CMD ["celery", "-A", "app.worker", "worker", "--loglevel=info"]
