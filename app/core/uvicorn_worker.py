"""Кастомный Gunicorn-worker — UvicornWorker с явно зафиксированными
loop=uvloop и http=httptools.

Стандартный uvicorn.workers.UvicornWorker использует CONFIG_KWARGS = {
    "loop": "auto", "http": "auto"
}. На большинстве production-Linux это даёт uvloop+httptools (они есть
в uvicorn[standard]), но при конфликте версий asyncio или непредвиденном
порядке импорта uvicorn может незаметно откатиться на std-asyncio loop —
даёт -20-30% к latency без видимых ошибок в логах.

Явная фиксация исключает этот класс багов.

Используется в entrypoint.sh:
    --worker-class=app.core.uvicorn_worker.UvloopWorker
"""
from uvicorn.workers import UvicornWorker


class UvloopWorker(UvicornWorker):
    CONFIG_KWARGS = {"loop": "uvloop", "http": "httptools"}
