# app/modules/utility/routers/admin_system_health.py
"""
Deep health-checks для админа: ловят «тихие» поломки, которые НЕ видны
в обычном /health (он только проверяет, что процесс жив).

История появления (инцидент мая 2026, Левшин):
  tariff_cache._ensure_loaded падал на ImportError из-за неверного
  импорта `from app.core.database import sync_db_session` (функция
  жила только в tasks.py). Exception ловился через широкий except,
  логов не было, и worker 2 недели крутился с пустым кешем тарифов.
  Все 24 жильца через promote_auto_approved получали no_active_tariff,
  показания висели в auto_approved без reading_id.

  /health возвращал 200 OK всё это время. Никаких алертов.

Этот модуль — диагностический endpoint, который явно ПЫТАЕТСЯ выполнить
критичные операции (загрузить кеш, прочитать активный период, проверить
наличие активных тарифов) и сообщает по каждому компоненту: OK / FAIL.

Использование:
  - cron / monitoring дёргает GET /api/admin/system/health/deep
  - админ открывает в браузере (роль admin) — видит JSON с статусами
  - Sentry получает breadcrumb через capture_message если что-то fail

Не закрывает все будущие баги (новые компоненты придётся добавлять
сюда вручную), но закрывает класс «тихих ImportError'ов в singleton'ах».
"""
from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import RoleChecker
from app.modules.utility.models import (
    BillingPeriod, GSheetsImportRow, Tariff, User,
)

router = APIRouter(prefix="/api/admin/system", tags=["Admin System Health"])
logger = logging.getLogger(__name__)

# Только admin может смотреть deep-health (там видны внутренности кеша,
# счётчики таблиц и т.п.).
allow_admin = RoleChecker(["admin"])


async def _check_db_alive(db: AsyncSession) -> dict[str, Any]:
    """SELECT 1 — есть ли вообще коннект."""
    t0 = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        return {
            "name": "db_connection",
            "status": "ok",
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }
    except Exception as e:
        logger.exception("[HEALTH-DEEP] db_connection failed")
        return {
            "name": "db_connection", "status": "fail", "error": str(e),
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }


async def _check_active_period(db: AsyncSession) -> dict[str, Any]:
    """Должен быть ровно один активный период."""
    try:
        rows = (await db.execute(
            select(BillingPeriod.id, BillingPeriod.name)
            .where(BillingPeriod.is_active.is_(True))
        )).all()
        if len(rows) == 0:
            return {
                "name": "active_period", "status": "fail",
                "error": "Нет активного периода — gsheets promote и biling будут падать",
            }
        if len(rows) > 1:
            return {
                "name": "active_period", "status": "warn",
                "error": f"Несколько активных периодов: {[r[1] for r in rows]}",
            }
        return {
            "name": "active_period", "status": "ok",
            "period_id": rows[0][0], "period_name": rows[0][1],
        }
    except Exception as e:
        logger.exception("[HEALTH-DEEP] active_period check failed")
        return {"name": "active_period", "status": "fail", "error": str(e)}


async def _check_active_tariffs(db: AsyncSession) -> dict[str, Any]:
    """В БД должен быть хотя бы один активный тариф."""
    try:
        rows = (await db.execute(
            select(Tariff.id, Tariff.name)
            .where(Tariff.is_active.is_(True))
        )).all()
        if not rows:
            return {
                "name": "active_tariffs", "status": "fail",
                "error": "В БД нет активных тарифов — все расчёты выдают no_active_tariff",
            }
        return {
            "name": "active_tariffs", "status": "ok",
            "count": len(rows),
            "tariffs": [{"id": r[0], "name": r[1]} for r in rows],
        }
    except Exception as e:
        logger.exception("[HEALTH-DEEP] active_tariffs check failed")
        return {"name": "active_tariffs", "status": "fail", "error": str(e)}


def _check_tariff_cache() -> dict[str, Any]:
    """Главная проверка после инцидента мая 2026.

    Принудительно пытается загрузить кеш и сверяет, что в нём появились
    тарифы. Если кеш пустой — это означает silent ImportError в
    _ensure_loaded (тот самый баг, на котором сидел Левшин).
    """
    from app.modules.utility.services.tariff_cache import tariff_cache
    try:
        # Принудительная перезагрузка, чтобы поймать актуальное состояние.
        tariff_cache.invalidate()
        active = tariff_cache.get_all_active()
        stats = tariff_cache.stats()
        if not active:
            return {
                "name": "tariff_cache", "status": "fail",
                "error": (
                    "Кеш тарифов пустой после invalidate. Возможные причины: "
                    "ImportError в _ensure_loaded (см. логи), нет активных "
                    "тарифов в БД, проблема с DB-соединением sync-сессии."
                ),
                "stats": stats,
            }
        return {
            "name": "tariff_cache", "status": "ok",
            "active_count": len(active),
            "default_tariff_id": stats.get("default_tariff_id"),
            "tariff_ids": list(active.keys()),
        }
    except Exception as e:
        logger.exception("[HEALTH-DEEP] tariff_cache check failed")
        return {"name": "tariff_cache", "status": "fail", "error": str(e)}


async def _check_gsheets_stuck(db: AsyncSession) -> dict[str, Any]:
    """Сколько строк gsheets застряло в auto_approved без reading_id.

    Это «тихая утечка» — sync формально успешен, но MeterReading не
    создан, жилец не виден в финотчёте. Раньше такие строки накапливались
    неделями (см. инцидент Левшина). Считаем >0 как WARN, >50 как FAIL.
    """
    try:
        result = await db.execute(
            select(GSheetsImportRow.id).where(
                GSheetsImportRow.status == "auto_approved",
                GSheetsImportRow.reading_id.is_(None),
                GSheetsImportRow.matched_user_id.is_not(None),
                GSheetsImportRow.hot_water.is_not(None),
                GSheetsImportRow.cold_water.is_not(None),
            ).limit(200)
        )
        ids = [r[0] for r in result.all()]
        count = len(ids)
        if count == 0:
            return {"name": "gsheets_stuck_rows", "status": "ok", "count": 0}
        status = "warn" if count < 50 else "fail"
        return {
            "name": "gsheets_stuck_rows", "status": status,
            "count": count,
            "first_ids": ids[:10],
            "hint": (
                "auto_approved строки без MeterReading. Запустите "
                "/api/admin/gsheets/promote-auto-approved или проверьте "
                "tariff_cache (см. инцидент мая 2026)."
            ),
        }
    except Exception as e:
        logger.exception("[HEALTH-DEEP] gsheets_stuck check failed")
        return {"name": "gsheets_stuck_rows", "status": "fail", "error": str(e)}


async def _check_users_without_room(db: AsyncSession) -> dict[str, Any]:
    """Активные жильцы без комнаты — не смогут подать показания."""
    try:
        result = await db.execute(
            select(User.id, User.username).where(
                User.is_deleted.is_(False),
                User.room_id.is_(None),
                User.role == "user",
            ).limit(50)
        )
        rows = result.all()
        count = len(rows)
        if count == 0:
            return {"name": "users_without_room", "status": "ok", "count": 0}
        return {
            "name": "users_without_room", "status": "warn", "count": count,
            "examples": [{"id": r[0], "username": r[1]} for r in rows[:10]],
            "hint": "Эти жильцы не привязаны к комнате — не смогут подать показания.",
        }
    except Exception as e:
        logger.exception("[HEALTH-DEEP] users_without_room check failed")
        return {"name": "users_without_room", "status": "fail", "error": str(e)}


def _send_sentry(checks: list[dict]) -> None:
    """Шлём breadcrumb в Sentry если есть FAIL. Не падаем если Sentry не настроен."""
    fails = [c for c in checks if c.get("status") == "fail"]
    if not fails:
        return
    try:
        import sentry_sdk
        for c in fails:
            sentry_sdk.capture_message(
                f"[HEALTH-DEEP FAIL] {c['name']}: {c.get('error', '')[:200]}",
                level="error",
            )
    except Exception:
        # Sentry не настроен или нет интернета — это OK, deep-health не должен
        # сам падать только потому что Sentry недоступен.
        logger.warning("[HEALTH-DEEP] Sentry capture failed (not critical)")


@router.get("/health/deep")
async def health_deep(
    response: Response,
    current_user: User = Depends(allow_admin),
    db: AsyncSession = Depends(get_db),
):
    """Глубокая проверка состояния системы.

    Возвращает JSON со списком всех проверок и их статусами:
      ok    — компонент работает
      warn  — деградация, но не блокирующая (jamais 503)
      fail  — компонент сломан, влияет на бизнес-операции (HTTP 503)

    Если хоть одна fail — общий status="fail" и HTTP 503 (для мониторинга
    Uptime Robot / Prometheus). При warn HTTP остаётся 200, чтобы не
    шумел при некритичных деградациях (например, 3 жильца без комнаты).
    """
    t0 = time.perf_counter()
    checks: list[dict[str, Any]] = []

    # Базовые проверки БД и кеша — параллельно не нужно, очень быстро.
    checks.append(await _check_db_alive(db))
    checks.append(await _check_active_period(db))
    checks.append(await _check_active_tariffs(db))
    checks.append(_check_tariff_cache())
    checks.append(await _check_gsheets_stuck(db))
    checks.append(await _check_users_without_room(db))

    has_fail = any(c.get("status") == "fail" for c in checks)
    has_warn = any(c.get("status") == "warn" for c in checks)
    overall = "fail" if has_fail else ("warn" if has_warn else "ok")

    if has_fail:
        response.status_code = 503
        _send_sentry(checks)

    return {
        "status": overall,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "checks": checks,
    }
