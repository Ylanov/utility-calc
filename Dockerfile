# ==========================================
# ===== STAGE 1: BUILDER ===================
# ==========================================
# Используем легковесный slim-образ Python в качестве основы.
# Этот этап ('builder') предназначен исключительно для сборки зависимостей.
FROM python:3.13-slim-bookworm AS builder

# Устанавливаем переменные окружения для оптимизации работы Python и pip.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Устанавливаем системные зависимости, необходимые для компиляции Python-пакетов.
# --no-install-recommends предотвращает установку ненужных пакетов, экономя место.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    libcairo2-dev \
    libpango1.0-dev \
    libgdk-pixbuf2.0-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем быстрый установщик 'uv' из его официального образа для ускорения сборки.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Копируем файл с зависимостями и устанавливаем их.
# Этот слой кешируется и не будет пересобираться, если requirements.txt не изменился.
COPY requirements.txt ./
RUN uv pip install --system --no-cache -r requirements.txt

# ==========================================
# ===== STAGE 2: FINAL =====================
# ==========================================
# Начинаем финальный, чистый образ с той же основы.
FROM python:3.13-slim-bookworm

# Устанавливаем аналогичные переменные окружения.
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONOPTIMIZE=1

WORKDIR /app

# Устанавливаем только runtime-зависимости, необходимые для работы приложения.
# Включаем исправленные пакеты шрифтов (fonts-dejavu-core) для WeasyPrint.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    libffi8 \
    libcairo2 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-freefont-ttf \
    && rm -rf /var/lib/apt/lists/*

# Создаём пользователя с ограниченными правами для безопасного запуска приложения.
RUN useradd --create-home --shell /bin/bash appuser && \
    mkdir -p /app/static/generated_files && \
    chown -R appuser:appuser /app

# Копируем подготовленные Python-пакеты из стадии 'builder'.
COPY --from=builder /usr/local /usr/local

# Копируем весь исходный код и конфигурации приложения одной командой.
# Это небольшая оптимизация, которая объединяет несколько слоев в один.
COPY --chown=appuser:appuser \
    app/ app/ \
    templates/ templates/ \
    static/ static/ \
    alembic/ alembic/ \
    alembic.ini . \
    alembic_arsenal/ alembic_arsenal/ \
    alembic_arsenal.ini .

# Копируем наш entrypoint-скрипт и делаем его исполняемым.
COPY --chown=appuser:appuser entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Переключаемся на созданного пользователя.
USER appuser

# Открываем порт, который будет слушать Gunicorn.
EXPOSE 8000

# Устанавливаем entrypoint как команду по умолчанию для запуска контейнера.
CMD ["/entrypoint.sh"]