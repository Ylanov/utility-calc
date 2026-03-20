#!/bin/bash
set -e

# --- КОНФИГУРАЦИЯ ---

# 1. Определяем количество CPU (один раз)
CPU_COUNT=$(nproc)

# 2. Количество воркеров
if [ -n "$GUNICORN_WORKERS" ]; then
  WORKERS="$GUNICORN_WORKERS"
else
  # Оптимально для async приложений
  WORKERS=$((CPU_COUNT * 2))
fi

# 3. Ограничение воркеров (защита от перегрузки)
MAX_WORKERS=${GUNICORN_MAX_WORKERS:-24}
if [ "$WORKERS" -gt "$MAX_WORKERS" ]; then
  WORKERS=$MAX_WORKERS
fi

# Минимум 1 воркер
if [ "$WORKERS" -lt 1 ]; then
  WORKERS=1
fi

# 4. Потоки (threads)
THREADS=${GUNICORN_THREADS:-4}

# 5. Соединения (актуально для async)
WORKER_CONNECTIONS=${GUNICORN_WORKER_CONNECTIONS:-1000}

# 6. Таймауты (можно переопределять)
TIMEOUT=${GUNICORN_TIMEOUT:-120}
KEEP_ALIVE=${GUNICORN_KEEP_ALIVE:-5}

# 7. Max requests (анти memory leak)
MAX_REQUESTS=${GUNICORN_MAX_REQUESTS:-2000}
MAX_REQUESTS_JITTER=${GUNICORN_MAX_REQUESTS_JITTER:-200}

# --- ЛОГИ ---

echo "==================================================="
echo "Starting Gunicorn with resolved configuration..."
echo "CPUs available:              $CPU_COUNT"
echo "Workers:                     $WORKERS"
echo "Threads per worker:          $THREADS"
echo "Worker connections:          $WORKER_CONNECTIONS"
echo "Timeout:                     $TIMEOUT"
echo "Keep-alive:                  $KEEP_ALIVE"
echo "Max requests:                $MAX_REQUESTS"
echo "Max requests jitter:         $MAX_REQUESTS_JITTER"
echo "==================================================="

# --- ЗАПУСК ---

exec gunicorn app.main:app \
  --workers=$WORKERS \
  --threads=$THREADS \
  --worker-class=uvicorn.workers.UvicornWorker \
  --bind=0.0.0.0:8000 \
  --timeout=$TIMEOUT \
  --keep-alive=$KEEP_ALIVE \
  --worker-connections=$WORKER_CONNECTIONS \
  --max-requests=$MAX_REQUESTS \
  --max-requests-jitter=$MAX_REQUESTS_JITTER \
  --worker-tmp-dir /dev/shm \
  --log-level info \
  --access-logfile - \
  --error-logfile -