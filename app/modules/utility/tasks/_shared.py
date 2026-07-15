# Общее ядро пакета tasks: logger с историческим именем и хелперы совместимости.
# Перенесено из монолитного tasks.py механически (распил на пакет), поведение 1:1.
# ВАЖНО: этот модуль не должен импортировать модули-задачи пакета (цикл).

import logging

# sync_db_session жил исторически тут. Перенесли в app.core.database, чтобы
# tariff_cache мог импортировать его без circular dep (см. инцидент мая 2026:
# tariff_cache хотел `from app.core.database import sync_db_session`, но
# функция была только здесь → ImportError → cache навсегда пустой → все
# promote_auto_approved падали с no_active_tariff). Реэкспортируем имя для
# обратной совместимости — старые импорты `from app.modules.utility.tasks
# import sync_db_session` продолжают работать (реэкспорт в __init__ пакета).
from app.core.database import SessionLocalSync, sync_db_session  # noqa: F401

# Имя логгера оставляем историческим ("…utility.tasks", как у бывшего
# модуля-монолита), чтобы настройка логирования по имени продолжала работать.
logger = logging.getLogger(__name__.rsplit(".", 1)[0])


def get_sync_db():
    # Сохранён для обратной совместимости. В новом коде используй sync_db_session().
    return SessionLocalSync()
