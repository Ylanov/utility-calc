"""tasks.py — celery-задачи для ИИ-помощника (L5, L7).

L5: каждый час обходим свежие необработанные error_log и просим ИИ
    оценить root_cause/severity/suggested_action. Результат пишем в
    error_log.ai_analysis (JSONB).

L7: каждое утро 09:00 МСК — daily admin briefing: метрики за вчера
    через LLM → markdown → создаём notification.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import desc, func, or_, select

from app.core.database import AsyncSessionLocal
from app.core.time_utils import utcnow
from app.modules.llm import service as llm_service
from app.modules.llm.prompts import (
    build_daily_briefing_prompt,
    build_error_analysis_prompt,
)
from app.modules.utility.models import (
    ErrorLog,
    GSheetsImportRow,
    MeterReading,
    User,
)

logger = logging.getLogger(__name__)


# =====================================================================
# L5: AI-анализ свежих ошибок
# =====================================================================

# Не разбираем ошибки старше N дней — там уже неактуально.
_ERROR_ANALYSIS_MAX_AGE_DAYS = 7
# Не делаем больше N ошибок за один тик. L8: понижено с 10 до 3
# для соответствия Freemium-лимиту GigaChat Lite (248k токенов/мес).
# Один анализ ≈ 1500-2500 токенов → 3 × 4 запуска/день = ~30k/мес.
_ERROR_ANALYSIS_BATCH = 3


async def _analyze_errors_run() -> dict:
    """Async-версия. Возвращает stats для celery-логов."""
    async with AsyncSessionLocal() as db:
        ok, reason = await llm_service.is_available(db)
        if not ok:
            return {"skipped": True, "reason": reason}

        cutoff = utcnow() - timedelta(days=_ERROR_ANALYSIS_MAX_AGE_DAYS)
        rows = (await db.execute(
            select(ErrorLog)
            .where(
                ErrorLog.ai_analysis.is_(None),
                ErrorLog.resolved.is_(False),
                ErrorLog.occurred_at >= cutoff,
            )
            .order_by(desc(ErrorLog.occurred_at))
            .limit(_ERROR_ANALYSIS_BATCH)
        )).scalars().all()

        if not rows:
            return {"analyzed": 0, "skipped_no_rows": True}

        analyzed = 0
        failed = 0
        for err in rows:
            payload = _error_to_prompt_payload(err)
            messages = build_error_analysis_prompt(payload)
            try:
                res = await llm_service.ask(
                    db, messages,
                    purpose="error_analysis",
                    related_type="error_log",
                    related_id=err.id,
                    temperature=0.1,
                    max_tokens=600,
                )
            except Exception as e:
                logger.warning("[llm.tasks] error_analysis failed for err=%s: %s",
                               err.id, e)
                failed += 1
                continue
            if not res.ok:
                logger.info("[llm.tasks] error_analysis NOT ok for err=%s: %s",
                            err.id, res.error)
                failed += 1
                if "Daily budget exceeded" in (res.error or ""):
                    # Бюджет на сегодня кончился — выходим.
                    break
                continue
            ai = _parse_llm_json(res.text)
            ai["_raw_text"] = res.text
            err.ai_analysis = ai
            err.ai_analyzed_at = utcnow()
            err.ai_model = "llm-pilot"  # service-слой определит реальную модель
            analyzed += 1

        await db.commit()
        return {"analyzed": analyzed, "failed": failed, "total_batch": len(rows)}


def _error_to_prompt_payload(err: ErrorLog) -> dict:
    """Срезаем error_log в компактный JSON-payload для LLM.
    L8: для Freemium-токенов агрессивно режем — traceback 30 строк
    вместо 100, exc_message 800 символов вместо 1500.
    """
    return {
        "id": err.id,
        "occurred_at": str(err.occurred_at),
        "source": err.source,
        "http_method": err.http_method,
        "http_path": err.http_path,
        "http_status": err.http_status,
        "exc_type": err.exc_type,
        "exc_message": (err.exc_message or "")[:800],
        "traceback_tail": "\n".join((err.traceback or "").split("\n")[-30:]),
        "request_body": err.request_body,
        "investigation": err.investigation,
        "extra": err.extra,
    }


def _parse_llm_json(text: str) -> dict:
    """LLM иногда оборачивает JSON в ```json…``` или добавляет преамбулу.
    Достаём первый {…} блок и пытаемся распарсить.
    """
    s = (text or "").strip()
    # Убираем code-fence если есть.
    if s.startswith("```"):
        # ```json\n{...}\n```
        try:
            s = s.split("```", 2)[1]
            if s.startswith("json"):
                s = s[4:]
            s = s.strip().rstrip("`").strip()
        except Exception:
            pass
    # Берём первое {...}.
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        s = s[start:end + 1]
    try:
        return json.loads(s)
    except Exception:
        # Не распарсилось — возвращаем raw как root_cause.
        return {
            "root_cause": (text or "")[:500],
            "severity": "unknown",
            "suggested_action": None,
            "is_known_pattern": None,
            "confidence": None,
            "_parse_error": "LLM returned non-JSON; saved raw text",
        }


# =====================================================================
# L7: Daily admin briefing
# =====================================================================

async def _daily_briefing_run() -> dict:
    async with AsyncSessionLocal() as db:
        ok, reason = await llm_service.is_available(db)
        if not ok:
            return {"skipped": True, "reason": reason}

        # Метрики за вчера.
        now = utcnow()
        yesterday_start = datetime.combine(
            (now - timedelta(days=1)).date(), datetime.min.time(),
        )
        yesterday_end = datetime.combine(now.date(), datetime.min.time())

        new_errors = (await db.execute(
            select(func.count(ErrorLog.id)).where(
                ErrorLog.occurred_at >= yesterday_start,
                ErrorLog.occurred_at < yesterday_end,
            )
        )).scalar_one()
        errors_by_source = dict((await db.execute(
            select(ErrorLog.source, func.count(ErrorLog.id))
            .where(
                ErrorLog.occurred_at >= yesterday_start,
                ErrorLog.occurred_at < yesterday_end,
            )
            .group_by(ErrorLog.source)
        )).all())
        # Топ-5 свежих ошибок из копилки (детали) — кормим ИИ, чтобы он
        # предложил вероятную причину и КОНКРЕТНОЕ решение, а не просто
        # констатировал «есть ошибки». Берём последние по времени.
        recent_error_rows = (await db.execute(
            select(ErrorLog)
            .order_by(ErrorLog.occurred_at.desc())
            .limit(5)
        )).scalars().all()
        top_errors = [
            {
                "id": e.id,
                "source": e.source,
                "status": e.http_status,
                "method": e.http_method,
                "path": e.http_path,
                "type": e.exc_type,
                "message": (e.exc_message or "")[:300],
                "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
            }
            for e in recent_error_rows
        ]
        open_gsheets_conflicts = (await db.execute(
            select(func.count(GSheetsImportRow.id)).where(
                GSheetsImportRow.status == "conflict"
            )
        )).scalar_one()
        new_readings = (await db.execute(
            select(func.count(MeterReading.id)).where(
                MeterReading.created_at >= yesterday_start,
                MeterReading.created_at < yesterday_end,
            )
        )).scalar_one()

        # ─── Блок аномалий за вчера (раздел «⚠ На что обратить внимание») ───
        # ВАЖНО: считаем ТОЛЬКО реальные аномалии — по anomaly_score>=80
        # (высокий риск, тот же порог что у авто-approve gate). Раньше
        # считали любой anomaly_flags!=NULL, но это ловило служебные маркеры
        # (GSHEETS_IMPORT, AUTO_GENERATED, BASELINE, PENDING…) и давало
        # бессмысленные 155/155 «все показания = аномалия». score>=80 = то,
        # что действительно требует ручной проверки.
        anomaly_count = (await db.execute(
            select(func.count(MeterReading.id)).where(
                MeterReading.created_at >= yesterday_start,
                MeterReading.created_at < yesterday_end,
                MeterReading.anomaly_score >= 80,
            )
        )).scalar_one()
        format_suspect_count = (await db.execute(
            select(func.count(MeterReading.id)).where(
                MeterReading.created_at >= yesterday_start,
                MeterReading.created_at < yesterday_end,
                or_(MeterReading.hot_water > 99999,
                    MeterReading.cold_water > 99999),
            )
        )).scalar_one()
        # Дорогие квитанции (>30k ₽ — по опыту почти всегда баг данных).
        high_cost_rows = (await db.execute(
            select(MeterReading.id, MeterReading.total_cost, User.username)
            .join(User, MeterReading.user_id == User.id, isouter=True)
            .where(
                MeterReading.created_at >= yesterday_start,
                MeterReading.created_at < yesterday_end,
                MeterReading.total_cost > 30000,
            )
            .order_by(desc(MeterReading.total_cost))
            .limit(5)
        )).all()
        high_cost_list = [
            {"reading_id": rid, "total_cost": float(tc or 0), "username": un}
            for (rid, tc, un) in high_cost_rows
        ]

        metrics = {
            "period": yesterday_start.date().isoformat(),
            "new_errors": new_errors,
            "new_errors_by_source": errors_by_source,
            "open_gsheets_conflicts": open_gsheets_conflicts,
            "new_readings_created": new_readings,
            "anomaly_readings_yesterday": anomaly_count,
            "format_suspect_yesterday": format_suspect_count,
            "high_cost_readings_yesterday": high_cost_list,
            "top_errors": top_errors,
        }

        messages = build_daily_briefing_prompt(metrics)
        res = await llm_service.ask(
            db, messages,
            purpose="daily_briefing",
            temperature=0.3,
            max_tokens=800,
        )
        if not res.ok:
            logger.warning("[llm.tasks] daily_briefing failed: %s", res.error)
            return {"ok": False, "error": res.error}

        # Кладём результат в admin_notifications, если такая таблица есть.
        # На пилоте — просто логируем; полноценный notification — позже.
        logger.info(
            "[llm.tasks] daily_briefing OK (%d chars, %s ₽):\n%s",
            len(res.text or ""), res.cost_rub, (res.text or "")[:500],
        )
        return {
            "ok": True,
            "briefing_chars": len(res.text or ""),
            "cost_rub": float(res.cost_rub or 0),
            "briefing_preview": (res.text or "")[:500],
        }


# =====================================================================
# Celery sync wrappers (worker — sync процесс)
# =====================================================================

def run_analyze_errors_sync() -> dict:
    """Sync-обёртка для celery task. Открывает event-loop одноразово."""
    try:
        return asyncio.run(_analyze_errors_run())
    except Exception as e:
        logger.exception("[llm.tasks] analyze_errors crashed")
        return {"crashed": True, "error": str(e)}


def run_daily_briefing_sync() -> dict:
    try:
        return asyncio.run(_daily_briefing_run())
    except Exception as e:
        logger.exception("[llm.tasks] daily_briefing crashed")
        return {"crashed": True, "error": str(e)}


__all__ = ["run_analyze_errors_sync", "run_daily_briefing_sync"]
