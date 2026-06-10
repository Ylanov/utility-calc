"""Единый реестр показаний (Фаза 2 объединения, 2026-06-09).

ОДИН список из ДВУХ источников в общем формате:
  - боевые MeterReading (QR/приложение/норматив/уже-промоут-gsheets) за период;
  - буфер GSheetsImportRow (необработанные строки импорта до промоута).

Read-only union (сорт по дате, фильтры по источнику/поиску, пагинация). Действия
(утвердить/переназначить) остаются на существующих эндпоинтах — фронт дёргает их
по row_type+id. Биллинг-путь подачи НЕ трогаем (Путь B).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import RoleChecker
from app.modules.utility.models import (
    MeterReading, GSheetsImportRow, BillingPeriod,
)

router = APIRouter(prefix="/api/admin/registry", tags=["Admin Registry (unified)"])
allow_management = RoleChecker(["accountant", "admin", "financier"])


def _reading_source(flags: Optional[str]) -> tuple[str, str]:
    """Источник боевого показания по anomaly_flags (явного столбца source нет)."""
    f = (flags or "").upper()
    if "GSHEETS" in f:
        return "gsheets", "📄 Google Sheets"
    if "MANUAL_RECEIPT" in f:
        return "manual", "✍️ Вручную"
    if any(a in f for a in ("AUTO_NORM", "AUTO_AVG", "AUTO_GENERATED",
                            "AUTO_NO_HISTORY", "STATIC_RENT")):
        return "auto", "🤖 Норматив/авто"
    return "user", "📱 QR/приложение"


@router.get("")
async def unified_registry(
    period_id: Optional[int] = Query(None),
    source: Optional[str] = Query(None, description="user|gsheets|auto|manual|buffer"),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user=Depends(allow_management),
    db: AsyncSession = Depends(get_db),
):
    period = None
    if period_id:
        period = await db.get(BillingPeriod, period_id)
    if not period:
        period = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active)
        )).scalars().first()

    items: list[dict] = []

    # --- Боевые MeterReading за период ---
    if period:
        mr = (await db.execute(
            select(MeterReading)
            .options(selectinload(MeterReading.user), selectinload(MeterReading.room))
            .where(MeterReading.period_id == period.id)
        )).scalars().all()
        for r in mr:
            src, label = _reading_source(r.anomaly_flags)
            room = r.room
            items.append({
                "row_type": "reading", "id": r.id,
                "source": src, "source_label": label,
                "timestamp": r.created_at.isoformat() if r.created_at else None,
                "fio": r.user.username if r.user else "—",
                "dormitory": (room.dormitory_name if room else None),
                "room": ((room.room_number or room.apartment_number) if room else None),
                "hot": str(r.hot_water) if r.hot_water is not None else None,
                "cold": str(r.cold_water) if r.cold_water is not None else None,
                "elect": str(r.electricity) if r.electricity is not None else None,
                "status": "approved" if r.is_approved else "draft",
                "sum": float(r.total_cost or 0),
                "anomaly_score": int(r.anomaly_score or 0),
                "matched": None,
            })

    # --- Буфер GSheetsImportRow (необработанные, до промоута) ---
    gs = (await db.execute(
        select(GSheetsImportRow).where(
            GSheetsImportRow.reading_id.is_(None),
            GSheetsImportRow.status.in_(["pending", "conflict", "unmatched", "auto_approved"]),
        )
    )).scalars().all()
    for g in gs:
        items.append({
            "row_type": "gsheets", "id": g.id,
            "source": "buffer", "source_label": "📄 Google Sheets (буфер)",
            "timestamp": (g.sheet_timestamp.isoformat() if g.sheet_timestamp
                          else (g.created_at.isoformat() if g.created_at else None)),
            "fio": g.raw_fio,
            "dormitory": g.raw_dormitory,
            "room": g.raw_room_number,
            "hot": str(g.hot_water) if g.hot_water is not None else None,
            "cold": str(g.cold_water) if g.cold_water is not None else None,
            "elect": None,
            "status": g.status,
            "sum": None,
            "anomaly_score": None,
            "matched": {
                "user_id": g.matched_user_id,
                "score": int(g.match_score or 0),
                "reason": g.conflict_reason,
            },
        })

    # --- Фильтры ---
    if source:
        items = [x for x in items if x["source"] == source]
    if search:
        s = search.lower()
        items = [x for x in items
                 if s in (x["fio"] or "").lower() or s in str(x.get("room") or "").lower()]

    # --- Сорт по дате (свежие сверху) + пагинация ---
    items.sort(key=lambda x: x["timestamp"] or "", reverse=True)
    total = len(items)
    start = (page - 1) * limit
    return {
        "items": items[start:start + limit],
        "total": total, "page": page, "limit": limit,
        "period": period.name if period else None,
        "period_id": period.id if period else None,
    }
