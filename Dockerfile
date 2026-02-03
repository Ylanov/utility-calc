# Используем slim версию
FROM python:3.10-slim

# Рабочая директория
WORKDIR /app

# 1. УСТАНОВКА СИСТЕМНЫХ ЗАВИСИМОСТЕЙ
# Исправлены имена пакетов для Debian Bookworm/Trixie
RUN apt-get update && apt-get install -y \
    postgresql-client \
    build-essential \
    python3-dev \
    python3-pip \
    python3-cffi \
    python3-brotli \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz-subset0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# 2. Установка Python библиотек
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Копируем код
COPY . .

# Запуск
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]