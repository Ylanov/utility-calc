"""celery_tasks.py — celery task-обёртки над async-логикой из tasks.py.

Делаем отдельный модуль чтобы celery.imports загружал только эти лёгкие
обёртки, а тяжёлые зависимости (httpx, cryptography) грузились лениво
внутри tasks.py через try/except.
"""
from __future__ import annotations

import logging

from app.worker import celery

logger = logging.getLogger(__name__)


@celery.task(name="llm_analyze_errors_task")
def llm_analyze_errors_task():
    """L5: разбор свежих error_log через LLM. Запускается каждый час."""
    try:
        from app.modules.llm.tasks import run_analyze_errors_sync
        result = run_analyze_errors_sync()
        logger.info("[llm_analyze_errors_task] %s", result)
        return result
    except Exception as e:
        logger.exception("[llm_analyze_errors_task] crashed")
        return {"crashed": True, "error": str(e)}


@celery.task(name="llm_daily_briefing_task")
def llm_daily_briefing_task():
    """L7: ежедневная утренняя сводка через LLM."""
    try:
        from app.modules.llm.tasks import run_daily_briefing_sync
        result = run_daily_briefing_sync()
        logger.info("[llm_daily_briefing_task] %s", result)
        return result
    except Exception as e:
        logger.exception("[llm_daily_briefing_task] crashed")
        return {"crashed": True, "error": str(e)}
