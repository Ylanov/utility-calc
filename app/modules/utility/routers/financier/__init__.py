"""Финансовый роутер (долги 1С, ГИС ГМП, отчётность) — пакет.

Монолитный routers/financier.py (~6.6k строк) распилен на модули ЧИСТО
МЕХАНИЧЕСКИ: поведение, пути роутов, сигнатуры и тексты ошибок не менялись.

Единый APIRouter создаётся в _shared.py; импорты модулей ниже РЕГИСТРИРУЮТ
его эндпоинты, поэтому порядок импортов повторяет порядок секций исходного
файла — НЕ пересортировывать (важно для порядка регистрации роутов).
"""

from ._shared import router

# Порядок = порядок секций монолитного financier.py. НЕ сортировать!
from . import debts_import  # noqa: F401 — Импорт долгов из Excel 1С
from . import gisgmp  # noqa: F401 — ГИС ГМП
from . import onec  # noqa: F401 — 1С (БГУ)
from . import gisgmp_reconcile  # noqa: F401 — ГИС ГМП
from . import debts_staged  # noqa: F401 — Черновики долгов 1С
from . import gisgmp_actualize  # noqa: F401 — ГИС ГМП
from . import debts_reports  # noqa: F401 — Отчётность по долгам
from . import debts_history  # noqa: F401 — История импортов 1С
from . import debts_integrity  # noqa: F401 — Диагностика
from . import debts_match  # noqa: F401 — Не найденные ФИО и обслуживание истории
from . import misc_finance  # noqa: F401 — Прочее финансовое

__all__ = ["router"]
