#!/bin/bash
set -e

CPU_COUNT=$(nproc)

# Оптимальная формула
if [ -n "$GUNICORN_WORKERS" ]; then
  WORKERS="$GUNICORN_WORKERS"
else
  WORKERS=$((CPU_COUNT * 2 + 1))
fi

# ЖЁСТКОЕ ограничение (очень важно)
MAX_WORKERS=${GUNICORN_MAX_WORKERS:-6}
if [ "$WORKERS" -gt "$MAX_WORKERS" ]; then
  WORKERS=$MAX_WORKERS
fi

if [ "$WORKERS" -lt 1 ]; then
  WORKERS=1
fi

THREADS=${GUNICORN_THREADS:-2}

TIMEOUT=${GUNICORN_TIMEOUT:-120}
KEEP_ALIVE=${GUNICORN_KEEP_ALIVE:-5}

MAX_REQUESTS=${GUNICORN_MAX_REQUESTS:-2000}
MAX_REQUESTS_JITTER=${GUNICORN_MAX_REQUESTS_JITTER:-200}

echo "Workers: $WORKERS | Threads: $THREADS | CPU: $CPU_COUNT"

exec gunicorn app.main:app \
  --workers=$WORKERS \
  --threads=$THREADS \
  --worker-class=uvicorn.workers.UvicornWorker \
  --bind=0.0.0.0:8000 \
  --timeout=$TIMEOUT \
  --keep-alive=$KEEP_ALIVE \
  --max-requests=$MAX_REQUESTS \
  --max-requests-jitter=$MAX_REQUESTS_JITTER \
  --worker-tmp-dir /dev/shm \
  --log-level info \
  --access-logfile - \
  --error-logfile -