"""Отчёты/аналитика админа (квитанции, экспорты, сводки, 360°) — пакет.

Монолитный routers/admin_reports.py (~2.5k строк) распилен на модули ЧИСТО
МЕХАНИЧЕСКИ: поведение, пути роутов, сигнатуры и тексты ошибок не менялись.

Единый APIRouter создаётся в _shared.py; импорты модулей ниже РЕГИСТРИРУЮТ
его эндпоинты, поэтому порядок импортов повторяет порядок секций исходного
файла — НЕ пересортировывать (важно для порядка регистрации роутов).
"""

from ._shared import router

# Порядок = порядок секций монолитного admin_reports.py. НЕ сортировать!
from . import receipts  # noqa: F401 — PDF квитанций (просмотр/стриминг)
from . import exports  # noqa: F401 — Excel-ведомость и выгрузка в 1С
from . import receipt_tasks  # noqa: F401 — фоновые задачи генерации/ZIP
from . import summary  # noqa: F401 — сводки v1/v2 + диагностика холостяков
from . import resident_detail  # noqa: F401 — история/финдетали/360°/баланс
from . import explain  # noqa: F401 — трассировка расчёта reading

# Реэкспорт для внешних потребителей (public_portal импортирует
# _compute_user_balance напрямую из admin_reports).
from .resident_detail import (  # noqa: F401
    _compute_user_balance,
    get_resident_finance_detail,
)

__all__ = ["router", "_compute_user_balance", "get_resident_finance_detail"]
