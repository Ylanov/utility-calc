#!/bin/bash

# 1. Определяем количество доступных процессорных ядер.
CPU_COUNT=$(nproc)

# 2. Рассчитываем количество воркеров по более безопасной для памяти формуле (CPU + 1).
# Это оптимальный баланс для I/O-bound приложений с тяжелыми задачами,
# чтобы избежать риска Out-Of-Memory (OOM) ошибок.
WORKERS=$((CPU_COUNT + 1))

# 3. Устанавливаем разумный верхний предел для воркеров.
# Защищает от неконтролируемого роста процессов на машинах с большим количеством ядер.
MAX_WORKERS=12
if [ "$WORKERS" -gt "$MAX_WORKERS" ]; then
  WORKERS=$MAX_WORKERS
fi

# 4. Устанавливаем количество потоков на каждого воркера.
# Позволяет одному процессу-воркеру обрабатывать несколько запросов одновременно,
# что улучшает утилизацию CPU и снижает потребление RAM по сравнению
# с увеличением количества самих процессов.
THREADS=2

# 5. Выводим итоговые параметры в лог для удобства отладки.
echo "==================================================="
echo "Starting Gunicorn with dynamic configuration..."
echo "CPUs available:              $CPU_COUNT"
echo "Calculated workers:          $WORKERS"
echo "Threads per worker:          $THREADS"
echo "Worker connections:          1000"
echo "==================================================="

# 6. Запускаем Gunicorn с полным набором production-ready параметров.
# `exec` гарантирует, что Gunicorn станет главным процессом (PID 1),
# что позволяет ему корректно получать сигналы от Docker (например, SIGTERM).
exec gunicorn app.main:app \
  --workers=$WORKERS \
  --threads=$THREADS \
  --worker-class=uvicorn.workers.UvicornWorker \
  --bind=0.0.0.0:8000 \
  --timeout=120 \
  --keep-alive=5 \
  --worker-connections=1000