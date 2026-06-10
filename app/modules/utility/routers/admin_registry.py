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

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import RoleChecker
from app.modules.utility.models import (
    MeterReading, GSheetsImportRow, BillingPeriod, Tariff, User,
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

    # Имена тарифов одним запросом. Эффективный тариф = тариф КОМНАТЫ
    # (room.tariff_id) с fallback на дефолтный id=1 — как в tariff_cache
    # (персональный User.tariff_id с roles_001 не учитывается).
    tariff_names = dict((await db.execute(select(Tariff.id, Tariff.name))).all())
    default_tariff = tariff_names.get(1) or "Базовый тариф"

    def _room_tariff(room) -> Optional[str]:
        if not room:
            return None
        return tariff_names.get(room.tariff_id) or default_tariff

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
                "tariff": _room_tariff(room),
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

    # Сопоставленные жильцы буфера — ФИО/комната/тариф одним запросом
    # (в старом gsheets-UI была колонка «Сопоставлено» — возвращаем её данные).
    matched_ids = [g.matched_user_id for g in gs if g.matched_user_id]
    matched_users: dict[int, User] = {}
    if matched_ids:
        for u in (await db.execute(
            select(User).options(selectinload(User.room))
            .where(User.id.in_(set(matched_ids)))
        )).scalars().all():
            matched_users[u.id] = u

    for g in gs:
        mu = matched_users.get(g.matched_user_id) if g.matched_user_id else None
        mu_room = mu.room if mu else None
        items.append({
            "row_type": "gsheets", "id": g.id,
            "source": "buffer", "source_label": "📄 Google Sheets (буфер)",
            "timestamp": (g.sheet_timestamp.isoformat() if g.sheet_timestamp
                          else (g.created_at.isoformat() if g.created_at else None)),
            "fio": g.raw_fio,
            "dormitory": g.raw_dormitory,
            "room": g.raw_room_number,
            "tariff": _room_tariff(mu_room),
            "hot": str(g.hot_water) if g.hot_water is not None else None,
            "cold": str(g.cold_water) if g.cold_water is not None else None,
            "elect": None,
            "status": g.status,
            "sum": None,
            "anomaly_score": None,
            "matched": {
                "user_id": g.matched_user_id,
                "fio": mu.username if mu else None,
                "room": ((mu_room.room_number or mu_room.apartment_number)
                         if mu_room else None),
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


class RejectBody(BaseModel):
    reason: Optional[str] = Field(None, max_length=300)


@router.post("/readings/{reading_id}/reject")
async def reject_reading(
    reading_id: int,
    body: RejectBody,
    current_user: User = Depends(allow_management),
    db: AsyncSession = Depends(get_db),
):
    """Отклонить ЧЕРНОВИК боевого показания: запись удаляется (жилец подаст
    заново — так же рекомендует анализатор), жильцу уходит уведомление в
    переписку QR-портала. Утверждённые не отклоняем — сначала «вернуть в
    черновик» (unapprove), иначе админ случайно снёс бы готовую квитанцию."""
    reading = (await db.execute(
        select(MeterReading).options(selectinload(MeterReading.period))
        .where(MeterReading.id == reading_id)
    )).scalars().first()
    if not reading:
        raise HTTPException(404, "Показание не найдено")
    if reading.is_approved:
        raise HTTPException(
            400, "Показание уже утверждено. Сначала верните его в черновик.")

    user_id = reading.user_id
    period_name = reading.period.name if reading.period else None

    # delete_reading: отвязка gsheets-строк + audit_log со снапшотом + commit.
    from app.modules.utility.services.admin_readings_manual import delete_reading
    await delete_reading(db, reading_id, actor=current_user)

    if user_id:
        from app.modules.utility.services.qr_portal import notify_reading_rejected
        notify_reading_rejected(db, user_id, period_name, body.reason)
        await db.commit()
    return {"status": "rejected"}
