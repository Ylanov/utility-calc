"""error_logger — общий модуль сохранения ошибок в БД (E3-A, 28.05.2026).

Сохраняет в таблицу error_log с автоматическим расследованием связанного
контекста по URL. Используется из:

  - app.core.middleware.error_capture — backend 500/4xx;
  - app.worker через task_failure signal — celery failures;
  - app.modules.utility.routers.admin_errors → POST /api/errors/frontend
    — JS-ошибки клиента.

Все вызовы — fire-and-forget с try/except внутри. Если сохранение ошибки
само упало (БД отвалилась, например), это не должно ломать request.
"""
from __future__ import annotations

import logging
import re
import traceback as _tb
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.utility.models import (
    AuditLog,
    ErrorLog,
    GSheetsImportRow,
    MeterReading,
    Room,
    User,
)

logger = logging.getLogger(__name__)


# =====================================================================
# Публичный API
# =====================================================================

async def log_error(
    db: AsyncSession,
    *,
    source: str,
    level: str = "error",
    http_method: Optional[str] = None,
    http_path: Optional[str] = None,
    http_status: Optional[int] = None,
    exc: Optional[BaseException] = None,
    exc_type: Optional[str] = None,
    exc_message: Optional[str] = None,
    traceback_str: Optional[str] = None,
    request_body: Any = None,
    user_id: Optional[int] = None,
    user_username: Optional[str] = None,
    request_id: Optional[str] = None,
    extra: Optional[dict] = None,
    run_investigation: bool = True,
) -> Optional[int]:
    """Сохранить ошибку в error_log. Возвращает id записи или None при сбое.

    Если передан `exc` (BaseException) — `exc_type` / `exc_message` /
    `traceback_str` извлекаются автоматически. Любые переданные явно
    значения имеют приоритет над извлечёнными.

    `run_investigation=True` (default) запускает _investigate_url —
    подгружает связанные сущности по URL. Для celery / frontend вызовов
    можно выключить (там URL может не быть).
    """
    if exc is not None:
        if exc_type is None:
            exc_type = type(exc).__name__
        if exc_message is None:
            exc_message = str(exc)[:5000]
        if traceback_str is None:
            traceback_str = "".join(
                _tb.format_exception(type(exc), exc, exc.__traceback__)
            )[:50000]

    investigation = None
    if run_investigation and http_path:
        try:
            investigation = await _investigate_url(
                db, http_path, http_method, user_id, request_body,
            )
        except Exception as inv_err:
            logger.warning(
                "[error_logger] investigation failed for %s: %s",
                http_path, inv_err,
            )
            investigation = {"_investigation_error": str(inv_err)[:500]}

    try:
        err = ErrorLog(
            source=source,
            level=level,
            http_method=http_method,
            http_path=http_path,
            http_status=http_status,
            exc_type=exc_type,
            exc_message=exc_message,
            traceback=traceback_str,
            request_body=_safe_jsonable(request_body),
            user_id=user_id,
            user_username=user_username,
            request_id=request_id,
            investigation=investigation,
            extra=_safe_jsonable(extra) if extra else None,
        )
        db.add(err)
        await db.commit()
        return err.id
    except Exception as e:
        logger.warning("[error_logger] save failed: %s", e)
        try:
            await db.rollback()
        except Exception:
            pass
        return None


# =====================================================================
# Авто-расследование по URL
# =====================================================================

# Шаблоны URL → handler-функция для подгрузки контекста.
# Порядок важен: первый match выигрывает.
_URL_PATTERNS = [
    (re.compile(r"^/api/admin/gsheets/rows/(?P<row_id>\d+)"), "gsheets_row"),
    (re.compile(r"^/api/admin/readings/(?P<reading_id>\d+)"), "reading"),
    (re.compile(r"^/api/admin/users/(?P<user_id>\d+)"), "user"),
    (re.compile(r"^/api/users/(?P<user_id>\d+)"), "user"),
    (re.compile(r"^/api/rooms/(?P<room_id>\d+)"), "room"),
]


async def _investigate_url(
    db: AsyncSession,
    path: str,
    method: Optional[str],
    user_id: Optional[int],
    body: Any,
) -> dict:
    """Подгрузить связанный контекст по URL для админ-разбора.

    Возвращает dict с ключами вида `gsheets_row`, `user`, `room`,
    `recent_readings`, `recent_audit`. Все запросы — best-effort; при
    сбое одного блока остальные продолжаются.
    """
    inv: dict = {}

    for pattern, kind in _URL_PATTERNS:
        m = pattern.match(path)
        if not m:
            continue
        try:
            if kind == "gsheets_row":
                await _enrich_gsheets_row(db, inv, int(m.group("row_id")))
            elif kind == "reading":
                await _enrich_reading(db, inv, int(m.group("reading_id")))
            elif kind == "user":
                await _enrich_user(db, inv, int(m.group("user_id")))
            elif kind == "room":
                await _enrich_room(db, inv, int(m.group("room_id")))
        except Exception as e:
            inv[f"_{kind}_error"] = str(e)[:300]
        break  # один match — достаточно

    # Дополнительно: recent audit-log для user_id из request.
    if user_id:
        try:
            recent = (await db.execute(
                select(AuditLog)
                .where(AuditLog.user_id == user_id)
                .order_by(AuditLog.created_at.desc())
                .limit(5)
            )).scalars().all()
            inv["recent_audit_by_requester"] = [_audit_to_dict(a) for a in recent]
        except Exception as e:
            inv["_audit_error"] = str(e)[:300]

    return inv


async def _enrich_gsheets_row(db: AsyncSession, inv: dict, row_id: int) -> None:
    row = await db.get(GSheetsImportRow, row_id)
    if not row:
        inv["gsheets_row"] = {"id": row_id, "_not_found": True}
        return
    inv["gsheets_row"] = {
        "id": row.id,
        "status": row.status,
        "conflict_reason": row.conflict_reason,
        "matched_user_id": row.matched_user_id,
        "matched_room_id": row.matched_room_id,
        "raw_fio": row.raw_fio,
        "raw_room_number": row.raw_room_number,
        "hot_water": str(row.hot_water) if row.hot_water is not None else None,
        "cold_water": str(row.cold_water) if row.cold_water is not None else None,
        "sheet_timestamp": str(row.sheet_timestamp) if row.sheet_timestamp else None,
        "reading_id": row.reading_id,
    }
    if row.matched_user_id:
        await _enrich_user(db, inv, row.matched_user_id)


async def _enrich_reading(db: AsyncSession, inv: dict, reading_id: int) -> None:
    r = await db.get(MeterReading, reading_id)
    if not r:
        inv["reading"] = {"id": reading_id, "_not_found": True}
        return
    inv["reading"] = {
        "id": r.id,
        "user_id": r.user_id,
        "room_id": r.room_id,
        "period_id": r.period_id,
        "hot_water": str(r.hot_water) if r.hot_water is not None else None,
        "cold_water": str(r.cold_water) if r.cold_water is not None else None,
        "electricity": str(r.electricity) if r.electricity is not None else None,
        "total_cost": str(r.total_cost) if r.total_cost is not None else None,
        "is_approved": r.is_approved,
        "anomaly_flags": r.anomaly_flags,
        "anomaly_score": r.anomaly_score,
        "created_at": str(r.created_at),
    }
    if r.user_id:
        await _enrich_user(db, inv, r.user_id)


async def _enrich_user(db: AsyncSession, inv: dict, user_id: int) -> None:
    u = await db.get(User, user_id)
    if not u:
        inv["user"] = {"id": user_id, "_not_found": True}
        return
    inv["user"] = {
        "id": u.id,
        "username": u.username,
        "role": u.role,
        "room_id": u.room_id,
        "residents_count": u.residents_count,
        "billing_mode": getattr(u, "billing_mode", None),
        "resident_type": getattr(u, "resident_type", None),
        "tariff_id": u.tariff_id,
        "is_deleted": u.is_deleted,
    }
    if u.room_id:
        await _enrich_room(db, inv, u.room_id)

    # Последние 5 reading'ов жильца.
    try:
        readings = (await db.execute(
            select(MeterReading)
            .where(MeterReading.user_id == u.id)
            .order_by(MeterReading.created_at.desc())
            .limit(5)
        )).scalars().all()
        inv["recent_readings"] = [
            {
                "id": r.id,
                "period_id": r.period_id,
                "hot_water": str(r.hot_water) if r.hot_water is not None else None,
                "cold_water": str(r.cold_water) if r.cold_water is not None else None,
                "anomaly_flags": r.anomaly_flags,
                "total_cost": str(r.total_cost) if r.total_cost is not None else None,
                "is_approved": r.is_approved,
                "created_at": str(r.created_at),
            }
            for r in readings
        ]
    except Exception as e:
        inv["_recent_readings_error"] = str(e)[:300]


async def _enrich_room(db: AsyncSession, inv: dict, room_id: int) -> None:
    r = await db.get(Room, room_id)
    if not r:
        inv["room"] = {"id": room_id, "_not_found": True}
        return
    inv["room"] = {
        "id": r.id,
        "place_type": r.place_type,
        "dormitory_name": r.dormitory_name,
        "room_number": r.room_number,
        "street": r.street,
        "house_number": r.house_number,
        "apartment_number": r.apartment_number,
        "format_address": r.format_address,
        "apartment_area": str(r.apartment_area) if r.apartment_area else None,
        "tariff_id": r.tariff_id,
        "is_vacant": r.is_vacant,
        "is_singles_apartment": getattr(r, "is_singles_apartment", None),
    }


def _audit_to_dict(a: AuditLog) -> dict:
    return {
        "id": a.id,
        "action": a.action,
        "entity_type": a.entity_type,
        "entity_id": a.entity_id,
        "username": getattr(a, "username", None),
        "created_at": str(a.created_at),
        "details": _safe_jsonable(getattr(a, "details", None)),
    }


# =====================================================================
# Утилиты
# =====================================================================

def _safe_jsonable(value: Any) -> Any:
    """Приводит value к JSON-serializable виду. Decimal/datetime → str.

    Защита: если value неконвертируемый (например byte-stream), возвращаем
    строку str(value)[:500].
    """
    if value is None:
        return None
    try:
        import json
        from datetime import date, datetime
        from decimal import Decimal

        def _default(o):
            if isinstance(o, (Decimal,)):
                return str(o)
            if isinstance(o, (datetime, date)):
                return o.isoformat()
            return str(o)

        # Сериализуем-десериализуем для проверки.
        return json.loads(json.dumps(value, default=_default, ensure_ascii=False))
    except Exception:
        try:
            return str(value)[:1000]
        except Exception:
            return None


__all__ = ["log_error"]
