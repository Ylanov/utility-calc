# app/modules/utility/routers/admin_gsheets.py
"""
Админские эндпоинты для управления импортом из Google Sheets.

POST  /api/admin/gsheets/sync                 — запустить синхронизацию (Celery)
GET   /api/admin/gsheets/rows                 — список импортированных строк
GET   /api/admin/gsheets/stats                — статистика по статусам
POST  /api/admin/gsheets/rows/{id}/approve    — утвердить строку → создать MeterReading
POST  /api/admin/gsheets/rows/{id}/reject     — отклонить строку
POST  /api/admin/gsheets/rows/{id}/reassign   — переназначить жильца (fuzzy не угадал)
POST  /api/admin/gsheets/rows/bulk-approve    — массовое утверждение
"""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.modules.utility.models import (
    GSheetsImportRow, MeterReading, User, BillingPeriod, Room,
)
from app.modules.utility.routers.admin_dashboard import write_audit_log

router = APIRouter(prefix="/api/admin/gsheets", tags=["Admin GSheets"])


# =========================================================================
# АВТОРИЗАЦИЯ
# =========================================================================
def require_admin(user: User) -> None:
    """Admin + accountant — могут управлять импортом. Остальные — нет."""
    if user.role not in ("admin", "accountant"):
        raise HTTPException(status_code=403, detail="Доступ запрещён")


# =========================================================================
# PYDANTIC SCHEMAS
# =========================================================================
class SyncRequest(BaseModel):
    sheet_id: Optional[str] = None  # если не задан — берём из settings
    gid: Optional[str] = None
    limit: Optional[int] = None


class ReassignRequest(BaseModel):
    user_id: int


class BulkApproveRequest(BaseModel):
    row_ids: list[int]


class RowResponse(BaseModel):
    id: int
    sheet_timestamp: Optional[datetime]
    raw_fio: str
    raw_dormitory: Optional[str]
    raw_room_number: Optional[str]
    hot_water: Optional[Decimal]
    cold_water: Optional[Decimal]
    matched_user_id: Optional[int]
    matched_username: Optional[str]
    matched_room: Optional[str]
    match_score: int
    status: str
    conflict_reason: Optional[str]
    reading_id: Optional[int]
    created_at: Optional[datetime]


# =========================================================================
# SYNC
# =========================================================================
@router.post("/sync")
async def trigger_sync(
    data: SyncRequest,
    current_user: User = Depends(get_current_user),
):
    """Запускает синхронизацию через Celery. Возвращает task_id."""
    require_admin(current_user)

    sheet_id = (data.sheet_id or settings.GSHEETS_SHEET_ID or "").strip()
    if not sheet_id:
        raise HTTPException(
            status_code=400,
            detail="Не задан sheet_id (ни в запросе, ни в GSHEETS_SHEET_ID env).",
        )

    from app.modules.utility.tasks import sync_gsheets_task

    task = sync_gsheets_task.delay(
        sheet_id=sheet_id,
        gid=data.gid or settings.GSHEETS_GID or "0",
        limit=data.limit,
    )
    return {"task_id": task.id, "status": "queued"}


# =========================================================================
# STATS
# =========================================================================
@router.get("/stats")
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Счётчики по статусам + время последнего импорта."""
    require_admin(current_user)

    stats_rows = (await db.execute(
        select(GSheetsImportRow.status, func.count(GSheetsImportRow.id))
        .group_by(GSheetsImportRow.status)
    )).all()
    stats = {status: count for status, count in stats_rows}

    total = sum(stats.values())
    last_ts = (await db.execute(
        select(func.max(GSheetsImportRow.created_at))
    )).scalar_one_or_none()
    last_sheet_ts = (await db.execute(
        select(func.max(GSheetsImportRow.sheet_timestamp))
    )).scalar_one_or_none()

    return {
        "total": total,
        "by_status": stats,
        "last_import_at": last_ts,
        "last_sheet_timestamp": last_sheet_ts,
        "sheet_id_configured": bool(settings.GSHEETS_SHEET_ID),
        "auto_sync_interval_min": settings.GSHEETS_SYNC_INTERVAL_MINUTES,
    }


# Статусы, которые админу надо обработать (всё, что не approved/rejected).
ACTIVE_STATUSES = ("pending", "unmatched", "conflict")
ARCHIVE_STATUSES = ("approved", "auto_approved", "rejected")


# =========================================================================
# LIST ROWS
# =========================================================================
@router.get("/rows")
async def list_rows(
    status: Optional[str] = Query(None, description="pending|unmatched|conflict|approved|rejected|auto_approved"),
    active_only: bool = Query(True, description="Если true — скрыть approved/auto_approved/rejected"),
    search: Optional[str] = Query(None, description="поиск по ФИО или комнате"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_admin(current_user)

    base = (
        select(GSheetsImportRow)
        .options(
            selectinload(GSheetsImportRow.matched_user).selectinload(User.room),
            selectinload(GSheetsImportRow.matched_room),
        )
    )
    count_q = select(func.count(GSheetsImportRow.id))

    if status:
        # Явно указанный статус перекрывает active_only.
        base = base.where(GSheetsImportRow.status == status)
        count_q = count_q.where(GSheetsImportRow.status == status)
    elif active_only:
        # По умолчанию показываем только то, что требует действий —
        # утверждённые и отклонённые уходят в "архив" (видны при
        # отключении флажка или явном выборе статуса).
        base = base.where(GSheetsImportRow.status.in_(ACTIVE_STATUSES))
        count_q = count_q.where(GSheetsImportRow.status.in_(ACTIVE_STATUSES))

    if search:
        pat = f"%{search.strip()}%"
        base = base.where(
            (GSheetsImportRow.raw_fio.ilike(pat)) |
            (GSheetsImportRow.raw_room_number.ilike(pat))
        )
        count_q = count_q.where(
            (GSheetsImportRow.raw_fio.ilike(pat)) |
            (GSheetsImportRow.raw_room_number.ilike(pat))
        )

    total = (await db.execute(count_q)).scalar_one()

    offset = (page - 1) * limit
    rows = (await db.execute(
        base.order_by(
            # Сначала pending/conflict — их надо обработать,
            # потом unmatched, потом уже утверждённые/отклонённые.
            # (PostgreSQL order by expression)
            (GSheetsImportRow.status == "pending").desc(),
            (GSheetsImportRow.status == "conflict").desc(),
            GSheetsImportRow.sheet_timestamp.desc().nulls_last(),
            GSheetsImportRow.created_at.desc(),
        )
        .offset(offset).limit(limit)
    )).scalars().all()

    items = []
    for r in rows:
        items.append({
            "id": r.id,
            "sheet_timestamp": r.sheet_timestamp,
            "raw_fio": r.raw_fio,
            "raw_dormitory": r.raw_dormitory,
            "raw_room_number": r.raw_room_number,
            "hot_water": r.hot_water,
            "cold_water": r.cold_water,
            "matched_user_id": r.matched_user_id,
            "matched_username": r.matched_user.username if r.matched_user else None,
            "matched_room": (
                f"{r.matched_user.room.dormitory_name}, ком. {r.matched_user.room.room_number}"
                if r.matched_user and r.matched_user.room else None
            ),
            "match_score": r.match_score or 0,
            "status": r.status,
            "conflict_reason": r.conflict_reason,
            "reading_id": r.reading_id,
            "created_at": r.created_at,
        })
    return {"total": total, "page": page, "size": limit, "items": items}


# =========================================================================
# APPROVE (создаёт MeterReading)
# =========================================================================
async def _apply_approve(
    db: AsyncSession,
    row: GSheetsImportRow,
    current_user: User,
) -> MeterReading:
    """
    Создаёт MeterReading на основании импортированной строки.
    Показание сразу помечается как is_approved=True (раз уж админ подтвердил).
    """
    if row.status in ("approved", "auto_approved") and row.reading_id:
        raise HTTPException(
            status_code=409,
            detail="Эта строка уже утверждена ранее",
        )

    if row.matched_user_id is None:
        raise HTTPException(
            status_code=400,
            detail="Строка не сопоставлена с жильцом — используйте reassign",
        )

    if row.hot_water is None or row.cold_water is None:
        raise HTTPException(
            status_code=400,
            detail="В импортированной строке не разобраны показания ГВС/ХВС",
        )

    user = await db.get(User, row.matched_user_id)
    if not user or user.is_deleted:
        raise HTTPException(status_code=400, detail="Жилец не найден или удалён")
    if not user.room_id:
        raise HTTPException(status_code=400, detail="Жилец не привязан к помещению")

    # Текущий активный период — к нему привяжем показание.
    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()

    # ЗАЩИТА ОТ ДУБЛЕЙ: если в этом периоде у жильца уже есть утверждённое
    # показание, не создаём второе. Админ увидит ошибку и может:
    #   - отклонить строку (если дубль случайный),
    #   - переназначить (если ошибка в матче),
    #   - удалить старое показание вручную и повторить.
    if active_period:
        duplicate = (await db.execute(
            select(MeterReading).where(
                MeterReading.user_id == user.id,
                MeterReading.period_id == active_period.id,
                MeterReading.is_approved.is_(True),
            ).limit(1)
        )).scalars().first()
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"У жильца уже есть утверждённое показание за период "
                    f"«{active_period.name}» (id={duplicate.id}). "
                    "Отклоните эту строку или удалите существующее показание."
                ),
            )

    reading = MeterReading(
        user_id=user.id,
        room_id=user.room_id,
        period_id=active_period.id if active_period else None,
        hot_water=row.hot_water,
        cold_water=row.cold_water,
        electricity=Decimal("0"),  # В Google Sheets нет электричества
        is_approved=True,
        anomaly_flags="GSHEETS_IMPORT",
        anomaly_score=0,
        total_cost=Decimal("0"),
        total_209=Decimal("0"),
        total_205=Decimal("0"),
    )
    db.add(reading)
    await db.flush()

    row.status = "approved"
    row.reading_id = reading.id
    row.processed_at = datetime.utcnow()
    row.processed_by_id = current_user.id

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gsheets_approve", entity_type="reading", entity_id=reading.id,
        details={"row_id": row.id, "fio": row.raw_fio},
    )
    return reading


@router.post("/rows/{row_id}/approve")
async def approve_row(
    row_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_admin(current_user)
    # SELECT FOR UPDATE — защита от race condition:
    # без него два админа могли одновременно нажать «утвердить» одну строку,
    # оба пройти проверку status != approved и создать дубль MeterReading.
    row = (await db.execute(
        select(GSheetsImportRow)
        .where(GSheetsImportRow.id == row_id)
        .with_for_update()
    )).scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail="Строка не найдена")
    reading = await _apply_approve(db, row, current_user)
    await db.commit()
    return {"status": "ok", "reading_id": reading.id}


@router.post("/rows/bulk-approve")
async def bulk_approve(
    data: BulkApproveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Массовое утверждение. Пропускает ошибочные строки, возвращает список проблемных."""
    require_admin(current_user)
    if not data.row_ids:
        return {"approved": 0, "failed": []}

    rows = (await db.execute(
        select(GSheetsImportRow).where(GSheetsImportRow.id.in_(data.row_ids))
    )).scalars().all()

    approved = 0
    failed = []
    for row in rows:
        try:
            await _apply_approve(db, row, current_user)
            approved += 1
        except HTTPException as e:
            failed.append({"row_id": row.id, "reason": e.detail})

    await db.commit()
    return {"approved": approved, "failed": failed}


# =========================================================================
# REJECT
# =========================================================================
@router.post("/rows/{row_id}/reject")
async def reject_row(
    row_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_admin(current_user)
    row = await db.get(GSheetsImportRow, row_id)
    if not row:
        raise HTTPException(status_code=404, detail="Строка не найдена")

    row.status = "rejected"
    row.processed_at = datetime.utcnow()
    row.processed_by_id = current_user.id

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gsheets_reject", entity_type="gsheets_row", entity_id=row_id,
        details={"fio": row.raw_fio},
    )
    await db.commit()
    return {"status": "ok"}


# =========================================================================
# REASSIGN
# =========================================================================
@router.post("/rows/{row_id}/reassign")
async def reassign_row(
    row_id: int,
    data: ReassignRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Переопределяет matched_user для строки (когда fuzzy не угадал)."""
    require_admin(current_user)

    row = await db.get(GSheetsImportRow, row_id)
    if not row:
        raise HTTPException(status_code=404, detail="Строка не найдена")

    new_user = await db.get(User, data.user_id)
    if not new_user or new_user.is_deleted:
        raise HTTPException(status_code=400, detail="Выбранный жилец не найден")

    row.matched_user_id = new_user.id
    row.matched_room_id = new_user.room_id
    row.match_score = 100  # ручной матч — максимальная уверенность
    row.status = "pending"  # возвращаем в очередь на утверждение
    row.conflict_reason = None

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gsheets_reassign", entity_type="gsheets_row", entity_id=row_id,
        details={"new_user_id": new_user.id, "fio": row.raw_fio},
    )
    await db.commit()
    return {"status": "ok"}


# =========================================================================
# USER HISTORY — комбинированная история подачи (GSheets + MeterReadings)
# =========================================================================
@router.get("/users/{user_id}/history")
async def user_history(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Полная история подач жильца:
    - все строки из GSheets (pending / approved / rejected ...)
    - все реальные утверждённые MeterReading из БД

    Нужно чтобы админ видел «общую картину» по жильцу и мог понять:
    - регулярно ли человек подаёт через таблицу;
    - совпадают ли значения с теми, что уходят в расчёт;
    - есть ли пропуски или дубли.
    """
    require_admin(current_user)

    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id == user_id)
    )).scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="Жилец не найден")

    # GSheets-строки, привязанные к этому жильцу
    gsheets_rows = (await db.execute(
        select(GSheetsImportRow)
        .where(GSheetsImportRow.matched_user_id == user_id)
        .order_by(GSheetsImportRow.sheet_timestamp.desc().nulls_last())
        .limit(100)
    )).scalars().all()

    # Реальные утверждённые показания за последний год
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(days=400)
    readings = (await db.execute(
        select(MeterReading)
        .options(selectinload(MeterReading.period))
        .where(
            MeterReading.user_id == user_id,
            MeterReading.is_approved.is_(True),
            MeterReading.created_at >= cutoff,
        )
        .order_by(MeterReading.created_at.desc())
        .limit(50)
    )).scalars().all()

    # Анализ: дата последней подачи в каждом источнике
    last_gsheet = gsheets_rows[0].sheet_timestamp if gsheets_rows else None
    last_reading = readings[0].created_at if readings else None

    # Расход последнего периода (если есть две точки)
    delta_hot = delta_cold = None
    if len(readings) >= 2:
        delta_hot = (readings[0].hot_water or 0) - (readings[1].hot_water or 0)
        delta_cold = (readings[0].cold_water or 0) - (readings[1].cold_water or 0)

    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "room": (
                f"{user.room.dormitory_name}, ком. {user.room.room_number}"
                if user.room else None
            ),
            "residents_count": user.residents_count,
        },
        "last_gsheet_submission": last_gsheet,
        "last_approved_reading": last_reading,
        "delta_hot": delta_hot,
        "delta_cold": delta_cold,
        "gsheets_rows": [
            {
                "id": r.id,
                "sheet_timestamp": r.sheet_timestamp,
                "hot_water": r.hot_water,
                "cold_water": r.cold_water,
                "status": r.status,
                "raw_room_number": r.raw_room_number,
                "conflict_reason": r.conflict_reason,
            }
            for r in gsheets_rows
        ],
        "approved_readings": [
            {
                "id": r.id,
                "created_at": r.created_at,
                "period": r.period.name if r.period else None,
                "hot_water": r.hot_water,
                "cold_water": r.cold_water,
                "electricity": r.electricity,
                "anomaly_flags": r.anomaly_flags,
            }
            for r in readings
        ],
    }


# =========================================================================
# DELETE (только полностью отброшенные)
# =========================================================================
@router.delete("/rows/{row_id}")
async def delete_row(
    row_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Физически удаляет строку. Доступно только для rejected/unmatched."""
    require_admin(current_user)
    row = await db.get(GSheetsImportRow, row_id)
    if not row:
        raise HTTPException(status_code=404, detail="Строка не найдена")
    if row.status in ("approved", "auto_approved"):
        raise HTTPException(
            status_code=400,
            detail="Нельзя удалить уже утверждённую строку — MeterReading создан",
        )
    await db.delete(row)
    await db.commit()
    return {"status": "ok"}
