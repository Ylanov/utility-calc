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
import re
from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.modules.utility.models import (
    GSheetsImportRow, MeterReading, User, BillingPeriod, Room, GSheetsAlias,
)
from app.modules.utility.routers.admin_dashboard import write_audit_log


# Используем единую нормализацию из gsheets_sync — раньше тут был свой
# «_normalize_fio» который НЕ убирал точки, а sync убирал. Получалось:
# после reassign alias сохранялся как «иванов и.и.», а sync на следующий
# месяц искал «иванов и и» — ключи никогда не совпадали, и каждый раз
# админ тыкал «Кто это?» заново. Теперь один источник правды.
from app.modules.utility.services.gsheets_sync import (
    normalize_fio as _normalize_fio,
    canonical_initials,
)

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
    # remember=True → создаём GSheetsAlias чтобы будущие подачи с таким ФИО
    # автоматически матчили этого жильца. По умолчанию True — это самая
    # частая желаемая семантика «ткнул правильного жильца → запомнил навсегда».
    remember: bool = True
    note: Optional[str] = None  # для UI: «жена», «брат», и т.д.


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
# PROMOTE AUTO-APPROVED — продвижение auto_approved в реальные MeterReading
# =========================================================================
# Обычно это делается автоматически в конце sync, но если старые данные
# ещё лежат без reading_id — этот endpoint можно дёрнуть вручную из UI.
@router.post("/promote-auto-approved")
async def trigger_promote_auto_approved(
    current_user: User = Depends(get_current_user),
):
    """Создаёт MeterReading для всех GSheetsImportRow со статусом
    auto_approved и reading_id=NULL.

    Запускается в отдельном thread-е (sync SQLAlchemy session), чтобы не
    блокировать event loop FastAPI на тысячах строк. Endpoint синхронен —
    клиент дожидается результата (создано/привязано/пропущено).
    """
    require_admin(current_user)

    import asyncio
    from app.modules.utility.tasks import sync_db_session
    from app.modules.utility.services.gsheets_sync import promote_auto_approved_rows

    def _run():
        with sync_db_session() as db:
            return promote_auto_approved_rows(db)

    return await asyncio.to_thread(_run)


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
# DASHBOARD — объединённый stats + rows одним запросом
# =========================================================================
# Раньше UI делал Promise.all([loadStats(), loadRows()]) — два HTTP-запроса
# на каждый refresh (включая авто-тик каждую минуту). Теперь один endpoint
# одной транзакцией возвращает оба набора. Фронт остаётся на старых /stats
# и /rows для совместимости (history-модалка, флатовая страница и т.п.).
@router.get("/dashboard")
async def get_dashboard(
    status: Optional[str] = Query(None),
    active_only: bool = Query(True),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает {stats, rows} одним вызовом."""
    require_admin(current_user)

    # --- STATS (тот же код, что в /stats) ---
    stats_rows_q = (await db.execute(
        select(GSheetsImportRow.status, func.count(GSheetsImportRow.id))
        .group_by(GSheetsImportRow.status)
    )).all()
    by_status = {s: c for s, c in stats_rows_q}
    last_ts = (await db.execute(
        select(func.max(GSheetsImportRow.created_at))
    )).scalar_one_or_none()
    last_sheet_ts = (await db.execute(
        select(func.max(GSheetsImportRow.sheet_timestamp))
    )).scalar_one_or_none()

    stats_payload = {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "last_import_at": last_ts,
        "last_sheet_timestamp": last_sheet_ts,
        "sheet_id_configured": bool(settings.GSHEETS_SHEET_ID),
        "auto_sync_interval_min": settings.GSHEETS_SYNC_INTERVAL_MINUTES,
    }

    # --- ROWS (тот же код, что в /rows) ---
    base = (
        select(GSheetsImportRow)
        .options(
            selectinload(GSheetsImportRow.matched_user).selectinload(User.room),
            selectinload(GSheetsImportRow.matched_room),
        )
    )
    count_q = select(func.count(GSheetsImportRow.id))

    if status:
        base = base.where(GSheetsImportRow.status == status)
        count_q = count_q.where(GSheetsImportRow.status == status)
    elif active_only:
        base = base.where(GSheetsImportRow.status.in_(ACTIVE_STATUSES))
        count_q = count_q.where(GSheetsImportRow.status.in_(ACTIVE_STATUSES))

    if search:
        pat = f"%{search.strip()}%"
        cond = (GSheetsImportRow.raw_fio.ilike(pat)) | (GSheetsImportRow.raw_room_number.ilike(pat))
        base = base.where(cond)
        count_q = count_q.where(cond)

    total = (await db.execute(count_q)).scalar_one()
    offset = (page - 1) * limit
    rows = (await db.execute(
        base.order_by(
            (GSheetsImportRow.status == "pending").desc(),
            (GSheetsImportRow.status == "conflict").desc(),
            GSheetsImportRow.sheet_timestamp.desc().nulls_last(),
            GSheetsImportRow.created_at.desc(),
        ).offset(offset).limit(limit)
    )).scalars().all()

    items = [{
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
    } for r in rows]

    return {
        "stats": stats_payload,
        "rows": {"total": total, "page": page, "size": limit, "items": items},
    }


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
    move_to_raw_room: bool = False,
) -> MeterReading:
    """
    Создаёт MeterReading на основании импортированной строки.
    Показание сразу помечается как is_approved=True (раз уж админ подтвердил).

    move_to_raw_room=True — если в таблице жилец указал другую комнату (conflict),
    сначала переводим его в новую комнату (User.room_id), а потом создаём
    показание на неё. Используется когда админ решает, что правильная —
    та, что в таблице, а не та, что сейчас у жильца.
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

    # Онлайн-перевод жильца в комнату из таблицы (при конфликте комнат).
    # Используем parse_room_number чтобы «00016» и «16» считать одним номером.
    if move_to_raw_room and row.raw_room_number:
        from app.modules.utility.services.gsheets_sync import parse_room_number
        raw_num = parse_room_number(row.raw_room_number)
        if raw_num:
            # Ищем комнату — приоритет в том же общежитии что и текущая у жильца,
            # либо по тексту raw_dormitory если указан.
            from sqlalchemy import func as _func
            target_room = None
            if user.room_id:
                current_room = await db.get(Room, user.room_id)
                if current_room:
                    target_room = (await db.execute(
                        select(Room).where(
                            Room.dormitory_name == current_room.dormitory_name,
                            _func.replace(Room.room_number, " ", "") == raw_num,
                        )
                    )).scalars().first()
            if not target_room and row.raw_dormitory:
                target_room = (await db.execute(
                    select(Room).where(
                        Room.dormitory_name.ilike(f"%{row.raw_dormitory.strip()}%"),
                        _func.replace(Room.room_number, " ", "") == raw_num,
                    )
                )).scalars().first()
            if not target_room:
                # Последний шанс — просто по номеру комнаты
                target_room = (await db.execute(
                    select(Room).where(
                        _func.replace(Room.room_number, " ", "") == raw_num
                    ).limit(1)
                )).scalars().first()
            if not target_room:
                raise HTTPException(
                    400,
                    f"Не нашёл комнату «{row.raw_dormitory or ''} {raw_num}» в базе. "
                    "Создайте помещение в Жилфонде или выберите вариант «Оставить текущую»."
                )
            user.room_id = target_room.id
            await db.flush()

    # Холостяк (per_capita) не подаёт показания счётчиков — платит фикс. сумму
    # за койко-место. Если всё-таки пришла подача от его ФИО (например, кто-то
    # в комнате сдал за всех) — это ошибка матчинга, а не их подача.
    # Отклоняем строку с понятным сообщением — админ должен переназначить
    # на другого жильца (например, family в той же комнате) или отклонить.
    if getattr(user, "billing_mode", "by_meter") == "per_capita":
        raise HTTPException(
            status_code=400,
            detail=(
                f"Жилец «{user.username}» оформлен на койко-место (per_capita) "
                "и физически не подаёт показания счётчиков. "
                "Используйте «Переназначить» чтобы привязать подачу к семейному жильцу этой комнаты."
            ),
        )

    # Текущий активный период — к нему привяжем показание.
    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()

    # ЗАЩИТА ОТ ДУБЛЕЙ: если в этом периоде у жильца уже есть утверждённое
    # показание, не создаём второе. Отдаём 409 со структурой conflict.
    #
    # ВАЖНО: именно `raise HTTPException(detail=dict)`, а не `return JSONResponse(...)`.
    # Раньше был второй вариант, но _apply_approve тогда возвращал Response
    # вместо MeterReading, и внешний approve_row падал с AttributeError при
    # попытке прочитать .id (и bulk_approve считал такой «возврат» как success).
    # HTTPException корректно всплывает через любой вызывающий код.
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
                detail={
                    "message": (
                        f"У жильца уже есть утверждённое показание за период "
                        f"«{active_period.name}» (id={duplicate.id}). "
                        "Отклоните эту строку или удалите существующее показание."
                    ),
                    "conflict": {
                        "user_username": user.username,
                        "period_name": active_period.name,
                        "row_id": row.id,
                        "existing": {
                            "id": duplicate.id,
                            "hot_water": float(duplicate.hot_water or 0),
                            "cold_water": float(duplicate.cold_water or 0),
                            "electricity": float(duplicate.electricity or 0),
                            "created_at": duplicate.created_at.isoformat() if duplicate.created_at else None,
                        },
                        "incoming": {
                            "hot_water": float(row.hot_water or 0),
                            "cold_water": float(row.cold_water or 0),
                        },
                    },
                },
            )

    # ЭЛЕКТРИЧЕСТВО: жильцы не подают электроэнергию через таблицу — колонки нет.
    # Берём последнее утверждённое значение этого жильца (показание счётчика
    # монотонно растёт, так что повтор предыдущего = «расход 0», что корректно
    # пока новой подачи нет). Если истории нет — ставим 0.
    prev_electricity = (await db.execute(
        select(MeterReading.electricity)
        .where(
            MeterReading.user_id == user.id,
            MeterReading.is_approved.is_(True),
        )
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    electricity_value = prev_electricity if prev_electricity is not None else Decimal("0")

    reading = MeterReading(
        user_id=user.id,
        room_id=user.room_id,
        period_id=active_period.id if active_period else None,
        hot_water=row.hot_water,
        cold_water=row.cold_water,
        electricity=electricity_value,
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
    move_to_raw_room: bool = Query(
        False,
        description="Если True — при conflict-комнат переселяем жильца "
                    "в комнату из таблицы (обновляем User.room_id).",
    ),
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
    reading = await _apply_approve(db, row, current_user, move_to_raw_room=move_to_raw_room)
    await db.commit()
    return {"status": "ok", "reading_id": reading.id}


@router.post("/rows/bulk-approve")
async def bulk_approve(
    data: BulkApproveRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Массовое утверждение. Пропускает ошибочные строки, возвращает список проблемных.

    ВАЖНО: обрабатываем строки СТРОГО по sheet_timestamp от старого к новому.
    Если админ утвердит сразу 29 подач Неметуллаевой за разные месяцы,
    MeterReading'и создадутся в хронологическом порядке — счётчики растут
    монотонно, дельты в истории корректные. Произвольный порядок SQL-ответа
    раньше давал путаницу с прошлой/последней подачей при выборке.
    """
    require_admin(current_user)
    if not data.row_ids:
        return {"approved": 0, "failed": []}

    rows = (await db.execute(
        select(GSheetsImportRow)
        .where(GSheetsImportRow.id.in_(data.row_ids))
        .order_by(
            GSheetsImportRow.sheet_timestamp.asc().nulls_last(),
            GSheetsImportRow.id.asc(),
        )
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
    """Переопределяет matched_user для строки (когда fuzzy не угадал).

    Важно: подтягиваем ВСЕ сестринские unmatched/conflict строки с таким же
    (нормализованным) ФИО и привязываем их тоже. Раньше приходилось тыкать
    «Кто это?» столько раз, сколько у жильца было подач в gsheets — админ
    тратил 10-20 кликов на одного человека. Теперь один reassign закрывает
    всю цепочку.
    """
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

    alias_created = False
    if data.remember:
        alias_created = await _ensure_alias(
            db,
            alias_fio=row.raw_fio,
            user_id=new_user.id,
            kind="manual",
            note=data.note,
            created_by_id=current_user.id,
        )

    # ------------------------------------------------------------
    # Подхват сестринских подач: все unmatched/conflict того же человека.
    # Матчим по ДВУМ формам одновременно:
    #   * полная нормализация (точное совпадение строки)
    #   * canonical_initials — «Иванов И.И.» и «Иванов Иван Иванович»
    #     считаются одним человеком.
    # Это главный фикс проблемы «каждый месяц заново жму Кто это».
    # ------------------------------------------------------------
    normalized_fio = _normalize_fio(row.raw_fio)
    canonical_fio = canonical_initials(row.raw_fio)
    siblings_updated = 0
    if normalized_fio or canonical_fio:
        sibling_rows = (await db.execute(
            select(GSheetsImportRow).where(
                GSheetsImportRow.id != row_id,
                GSheetsImportRow.status.in_(("unmatched", "conflict", "pending")),
            )
        )).scalars().all()
        for sr in sibling_rows:
            sr_norm = _normalize_fio(sr.raw_fio)
            sr_canon = canonical_initials(sr.raw_fio)
            # Считаем sibling-ом если совпал любой из видов нормализации
            if (sr_norm and sr_norm == normalized_fio) or (sr_canon and sr_canon == canonical_fio):
                sr.matched_user_id = new_user.id
                sr.matched_room_id = new_user.room_id
                sr.match_score = 100
                sr.status = "pending"
                sr.conflict_reason = None
                siblings_updated += 1

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gsheets_reassign", entity_type="gsheets_row", entity_id=row_id,
        details={
            "new_user_id": new_user.id,
            "fio": row.raw_fio,
            "alias_created": alias_created,
            "siblings_updated": siblings_updated,
        },
    )
    await db.commit()
    return {
        "status": "ok",
        "alias_created": alias_created,
        "siblings_updated": siblings_updated,
    }


async def _ensure_alias(
    db: AsyncSession,
    *,
    alias_fio: str,
    user_id: int,
    kind: str,
    note: Optional[str],
    created_by_id: int,
) -> bool:
    """Создаёт GSheetsAlias если его ещё нет. Возвращает True если действительно
    создал новую запись. Если такой alias уже привязан к ДРУГОМУ жильцу —
    оставляет старый (не перезаписываем молча, чтобы не было неожиданностей).
    """
    normalized = _normalize_fio(alias_fio)
    if not normalized:
        return False
    existing = (await db.execute(
        select(GSheetsAlias).where(GSheetsAlias.alias_fio_normalized == normalized)
    )).scalars().first()
    if existing:
        return False  # уже есть; если привязан к другому жильцу — это сознательно не трогаем
    db.add(GSheetsAlias(
        alias_fio=alias_fio.strip(),
        alias_fio_normalized=normalized,
        user_id=user_id,
        kind=kind,
        note=note,
        created_by_id=created_by_id,
    ))
    await db.flush()
    return True


# =========================================================================
# SEARCH USERS — для модалки переназначения (поиск по ФИО, не по ID)
# =========================================================================
@router.get("/search-users")
async def search_users(
    q: str = Query("", description="ФИО / часть ФИО / номер комнаты"),
    limit: int = Query(20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Подсказка для UI: найти жильца по ФИО или комнате.

    Поиск токенизированный с поддержкой инициалов. Запрос «Неметуллаева А. Р.»
    раньше ILIKE'ом не находил ничего (в БД хранится «Неметуллаева Айгуль
    Рустамовна»), теперь корректно матчит:
      * полнословные токены (len ≥ 2, без точки) → ILIKE %token% в AND
      * инициалы (1 буква, с точкой/без) → должно быть слово в username,
        начинающееся на эту букву

    Примеры:
      «неметуллаева»       → ILIKE %неметуллаева%
      «иванов и.»          → ILIKE %иванов% + есть слово на «и»
      «иванова а. р.»      → ILIKE %иванова% + слова на «а» и «р»
      «409»                → по номеру комнаты
    """
    require_admin(current_user)
    q = (q or "").strip()
    if len(q) < 2:
        return {"items": []}

    # Нормализация: убираем запятые/точки как границы слов, нижний регистр.
    import re as _re
    raw_tokens = [t for t in _re.split(r"[\s,]+", q) if t]

    full_words = []        # полные слова (≥2 букв после .strip('.'))
    initials = []          # одиночные буквы-инициалы
    for t in raw_tokens:
        clean = t.strip(".").lower()
        if not clean:
            continue
        if len(clean) == 1 and clean.isalpha():
            initials.append(clean)
        elif len(clean) >= 2:
            full_words.append(clean)

    # Если q — чистое число (номер комнаты) — отдельная ветка.
    is_numeric = q.replace(" ", "").isdigit()

    stmt = (
        select(User)
        .options(selectinload(User.room))
        .where(User.is_deleted.is_(False), User.role == "user")
    )

    if is_numeric:
        stmt = stmt.where(User.room.has(Room.room_number.ilike(f"%{q}%")))
    elif full_words:
        # AND по полнословным токенам — иначе «иванов петров» вернёт всех
        # у кого ЛИБО фамилия «иванов» ЛИБО имя «петров».
        for fw in full_words:
            stmt = stmt.where(User.username.ilike(f"%{fw}%"))
    elif initials:
        # Только инициалы — слишком размыто (вернёт пол-базы), не ищем.
        return {"items": []}
    else:
        return {"items": []}

    # Префетчим чуть больше чем limit — потом отфильтруем по инициалам в Python.
    rows = (await db.execute(stmt.limit(limit * 4))).scalars().all()

    # Python-рефайн по инициалам. Каждый инициал должен совпасть с первой
    # буквой какого-нибудь слова в username. Пример: ["а","р"] должны найтись
    # в ["неметуллаева","айгуль","рустамовна"] → «айгуль»→а, «рустамовна»→р.
    if initials:
        filtered = []
        for u in rows:
            words = u.username.lower().split()
            if all(any(w.startswith(i) for w in words) for i in initials):
                filtered.append(u)
        rows = filtered

    rows = rows[:limit]

    return {
        "items": [
            {
                "id": u.id,
                "username": u.username,
                "room": (
                    f"{u.room.dormitory_name}, ком. {u.room.room_number}"
                    if u.room else None
                ),
                "residents_count": u.residents_count,
            }
            for u in rows
        ]
    }


# =========================================================================
# RELATIVE CANDIDATES — анализатор «возможно, это супруга/родственник X?»
# =========================================================================
def _surname_of(fio: str) -> str:
    """Первое слово ФИО — фамилия. «Иванова Мария Петровна» → «иванова»."""
    parts = (fio or "").strip().split()
    return parts[0].lower() if parts else ""


def _patronymic_of(fio: str) -> str:
    """Третье слово ФИО — отчество. Может быть пустым (двусловное ФИО)."""
    parts = (fio or "").strip().split()
    return parts[2].lower() if len(parts) >= 3 else ""


def _surname_root(surname: str) -> str:
    """Корень фамилии без gender-окончаний.
    «Иванова» → «иванов», «Петровский» → «петровск», «Сидорова» → «сидоров».
    Также унифицирует национальные окончания: «Акопян/Акопянц», «Абуладзе»,
    «Гамцемлидзе/Гамсахурдия» — важно для общежитий в СНГ.
    Грубое приближение, но достаточно для фильтрации однофамильцев."""
    s = (surname or "").lower().strip()
    # Женские окончания — сначала (иначе «Иванова» схлопнется только до «Иванов» через «ов»,
    # чего мы и хотим, но «Иванова»→«ов» важнее чем «а»).
    for end in ("ова", "ева", "ёва", "ина", "ская", "цкая", "аева", "иева", "уева"):
        if s.endswith(end) and len(s) > len(end) + 1:
            return s[: -len(end)]
    # Мужские окончания
    for end in ("ов", "ев", "ёв", "ин", "ский", "цкий", "ой", "аев", "иев", "уев"):
        if s.endswith(end) and len(s) > len(end) + 1:
            return s[: -len(end)]
    # Национальные суффиксы — не урезаем, но нормализуем возможные вариации,
    # чтобы «Акопянц» и «Акопян» давали один корень.
    for end in ("янц", "ьянц"):
        if s.endswith(end) and len(s) > len(end) + 1:
            return s[: -len(end)] + "ян"
    # -идзе / -швили / -ия / -ко / -юк / -чук — не трогаем (мужской и женский вариант
    # совпадают, преобразование не нужно).
    return s


def _patronymic_root(patronymic: str) -> str:
    """Корень отчества — снимаем gender-окончания.
    «Петровна» → «петров», «Петрович» → «петров»."""
    p = (patronymic or "").lower().strip()
    for end in ("овна", "евна", "ична"):
        if p.endswith(end):
            return p[:-len(end)] + ("ов" if end[0] == "о" else "ев")
    for end in ("ович", "евич", "ич"):
        if p.endswith(end):
            return p[:-len(end)] + ("ов" if end[0] == "о" else "ев")
    return p


@router.get("/rows/{row_id}/relative-candidates")
async def relative_candidates(
    row_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает потенциальных «родственников» для unmatched-строки.

    Логика подсказок (от сильных к слабым):
      1) Жильцы в той же комнате что и raw_room_number.
         Сильнейший сигнал: жена и муж живут вместе.
      2) Жильцы с похожей фамилией (тот же корень) в том же общежитии.
      3) Жильцы у которых отчество имеет общий корень с отчеством из подачи —
         «Иванова Мария Петровна» подаёт за «Иванов Алексей Петрович» (брат).
    """
    require_admin(current_user)

    row = await db.get(GSheetsImportRow, row_id)
    if not row:
        raise HTTPException(status_code=404, detail="Строка не найдена")

    fio = row.raw_fio or ""
    surname_root = _surname_root(_surname_of(fio))
    patronymic_root = _patronymic_root(_patronymic_of(fio))
    raw_room = (row.raw_room_number or "").strip()
    raw_dorm = (row.raw_dormitory or "").strip()

    candidates: dict[int, dict] = {}

    def _add(user: User, reason: str, score: int):
        if user.id in candidates:
            # Если уже добавлен — повышаем score только если этот сильнее.
            if score > candidates[user.id]["score"]:
                candidates[user.id]["score"] = score
                candidates[user.id]["reason"] = reason
            return
        candidates[user.id] = {
            "id": user.id,
            "username": user.username,
            "room": (
                f"{user.room.dormitory_name}, ком. {user.room.room_number}"
                if user.room else None
            ),
            "residents_count": user.residents_count,
            "reason": reason,
            "score": score,
        }

    # ---------------- 1. Соседи по комнате ----------------
    # Раньше был строгий `Room.room_number == raw_room` — «409А»/« 409 » мимо.
    # Теперь нормализуем: убираем пробелы + используем ILIKE. Плюс отдельно
    # сравниваем только цифровую часть (чтобы «комн. 409» vs «409А» всё равно
    # давало roommate-хит).
    if raw_room:
        import re as _re2
        room_clean = raw_room.strip()
        room_digits = "".join(_re2.findall(r"\d+", room_clean))  # только цифры
        room_conds = [Room.room_number.ilike(f"%{room_clean}%")]
        if room_digits and room_digits != room_clean:
            room_conds.append(Room.room_number.ilike(f"%{room_digits}%"))

        room_query = select(User).options(selectinload(User.room)).where(
            User.is_deleted.is_(False),
            User.role == "user",
            User.room.has(or_(*room_conds)),
        )
        if raw_dorm:
            room_query = room_query.where(
                User.room.has(Room.dormitory_name.ilike(f"%{raw_dorm}%"))
            )
        roommates = (await db.execute(room_query.limit(15))).scalars().all()
        for u in roommates:
            same_surname = _surname_root(_surname_of(u.username)) == surname_root and surname_root
            _add(u, reason=("Сосед по комнате · однофамилец" if same_surname else "Сосед по комнате"),
                 score=95 if same_surname else 80)

    # ---------------- 2. Однофамильцы в общежитии ----------------
    if surname_root and len(surname_root) >= 3:
        # Раньше prefix-match `{root}%` — пропускал усеров с не-ФИО username
        # (логины вроде «nm_ivanov»). Теперь substring `%{root}%` + python-рефайн
        # чтобы отсечь ложные срабатывания (root «иван» совпадёт на «Ивановна»).
        sur_query = select(User).options(selectinload(User.room)).where(
            User.is_deleted.is_(False),
            User.role == "user",
            User.username.ilike(f"%{surname_root}%"),
        )
        if raw_dorm:
            sur_query = sur_query.where(
                User.room.has(Room.dormitory_name.ilike(f"%{raw_dorm}%"))
            )
        same_surname = (await db.execute(sur_query.limit(30))).scalars().all()
        for u in same_surname:
            # Основной фильтр: surname_root реально совпадает с корнем ПЕРВОГО
            # слова в username. Это убирает ложные срабатывания типа
            # «иван» matching «Ивановна» (там отчество, не фамилия).
            if _surname_root(_surname_of(u.username)) == surname_root:
                _add(u, reason="Однофамилец в общежитии", score=70)
                continue
            # Fallback: если username не в формате «Фамилия Имя ...» (login-стиль)
            # и содержит surname_root как подстроку — всё равно предлагаем,
            # но с пониженным score, пусть админ решит.
            if surname_root in u.username.lower():
                _add(u, reason="Похожая фамилия (возможно, в другом поле)", score=55)

    # ---------------- 3. Общий корень отчества ----------------
    if patronymic_root and len(patronymic_root) >= 4:
        # Любой жилец с похожим отчеством, в том же общежитии — возможный брат/сестра.
        pat_query = select(User).options(selectinload(User.room)).where(
            User.is_deleted.is_(False),
            User.role == "user",
            User.username.ilike(f"%{patronymic_root}%"),
        )
        if raw_dorm:
            pat_query = pat_query.where(
                User.room.has(Room.dormitory_name.ilike(f"%{raw_dorm}%"))
            )
        same_pat = (await db.execute(pat_query.limit(10))).scalars().all()
        for u in same_pat:
            if _patronymic_root(_patronymic_of(u.username)) != patronymic_root:
                continue
            _add(u, reason="Общее отчество (возможно, брат/сестра)", score=55)

    items = sorted(candidates.values(), key=lambda x: -x["score"])[:10]

    return {
        "row_id": row.id,
        "raw_fio": row.raw_fio,
        "raw_room": raw_room,
        "raw_dormitory": raw_dorm,
        "candidates": items,
    }


class ConfirmRelativeRequest(BaseModel):
    user_id: int
    note: Optional[str] = "Родственник"  # «жена», «муж», «сын» — для аудита


@router.post("/rows/{row_id}/confirm-relative")
async def confirm_relative(
    row_id: int,
    data: ConfirmRelativeRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Админ подтвердил «эта подача от родственника жильца X».
    Создаёт alias чтобы будущие подачи с таким ФИО автоматически матчили X.
    Текущая строка переходит в pending с matched_user_id = X."""
    require_admin(current_user)

    row = await db.get(GSheetsImportRow, row_id)
    if not row:
        raise HTTPException(status_code=404, detail="Строка не найдена")

    new_user = await db.get(User, data.user_id)
    if not new_user or new_user.is_deleted:
        raise HTTPException(status_code=400, detail="Выбранный жилец не найден")

    # Создаём alias (kind='relative' — для статистики «сколько связок подтверждено»)
    alias_created = await _ensure_alias(
        db,
        alias_fio=row.raw_fio,
        user_id=new_user.id,
        kind="relative",
        note=data.note,
        created_by_id=current_user.id,
    )
    if not alias_created:
        # Уже есть alias на это ФИО — возможно, привязан к другому жильцу.
        # Возвращаем 409 чтобы UI показал внятное «уже связано с Y, удалите алиас сначала».
        existing = (await db.execute(
            select(GSheetsAlias).options(selectinload(GSheetsAlias.user))
            .where(GSheetsAlias.alias_fio_normalized == _normalize_fio(row.raw_fio))
        )).scalars().first()
        if existing and existing.user_id != new_user.id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"ФИО «{row.raw_fio}» уже связано с жильцом "
                    f"«{existing.user.username}». Удалите старый alias и попробуйте снова."
                ),
            )

    row.matched_user_id = new_user.id
    row.matched_room_id = new_user.room_id
    row.match_score = 100
    row.status = "pending"
    row.conflict_reason = None

    # Подхват сестринских подач — аналогично reassign (по ДВУМ формам:
    # normalize + canonical_initials), чтобы разные форматы одного ФИО
    # не требовали повторных кликов «Кто это?».
    normalized_fio = _normalize_fio(row.raw_fio)
    canonical_fio = canonical_initials(row.raw_fio)
    siblings_updated = 0
    if normalized_fio or canonical_fio:
        sibling_rows = (await db.execute(
            select(GSheetsImportRow).where(
                GSheetsImportRow.id != row_id,
                GSheetsImportRow.status.in_(("unmatched", "conflict", "pending")),
            )
        )).scalars().all()
        for sr in sibling_rows:
            sr_norm = _normalize_fio(sr.raw_fio)
            sr_canon = canonical_initials(sr.raw_fio)
            if (sr_norm and sr_norm == normalized_fio) or (sr_canon and sr_canon == canonical_fio):
                sr.matched_user_id = new_user.id
                sr.matched_room_id = new_user.room_id
                sr.match_score = 100
                sr.status = "pending"
                sr.conflict_reason = None
                siblings_updated += 1

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gsheets_confirm_relative", entity_type="gsheets_row", entity_id=row_id,
        details={
            "alias_user_id": new_user.id,
            "alias_fio": row.raw_fio,
            "note": data.note,
            "siblings_updated": siblings_updated,
        },
    )
    await db.commit()
    return {
        "status": "ok",
        "alias_created": alias_created,
        "siblings_updated": siblings_updated,
    }


# =========================================================================
# ALIASES MANAGEMENT — список / удаление сохранённых связок
# =========================================================================
@router.get("/aliases")
async def list_aliases(
    user_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_admin(current_user)

    base = (
        select(GSheetsAlias)
        .options(selectinload(GSheetsAlias.user), selectinload(GSheetsAlias.created_by))
    )
    count_q = select(func.count(GSheetsAlias.id))

    if user_id:
        base = base.where(GSheetsAlias.user_id == user_id)
        count_q = count_q.where(GSheetsAlias.user_id == user_id)
    if search:
        pat = f"%{search.strip().lower()}%"
        base = base.where(GSheetsAlias.alias_fio_normalized.ilike(pat))
        count_q = count_q.where(GSheetsAlias.alias_fio_normalized.ilike(pat))

    total = (await db.execute(count_q)).scalar_one()
    rows = (await db.execute(
        base.order_by(GSheetsAlias.created_at.desc())
        .offset((page - 1) * limit).limit(limit)
    )).scalars().all()

    return {
        "total": total,
        "items": [
            {
                "id": a.id,
                "alias_fio": a.alias_fio,
                "user_id": a.user_id,
                "username": a.user.username if a.user else None,
                "kind": a.kind,
                "note": a.note,
                "created_at": a.created_at,
                "created_by": a.created_by.username if a.created_by else None,
            }
            for a in rows
        ],
    }


@router.delete("/aliases/{alias_id}")
async def delete_alias(
    alias_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удалить сохранённую связку. Будущие подачи с таким ФИО снова пойдут
    через fuzzy и попадут в unmatched/conflict."""
    require_admin(current_user)
    alias = await db.get(GSheetsAlias, alias_id)
    if not alias:
        raise HTTPException(status_code=404, detail="Алиас не найден")

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gsheets_alias_delete", entity_type="gsheets_alias", entity_id=alias_id,
        details={"alias_fio": alias.alias_fio, "user_id": alias.user_id},
    )
    await db.delete(alias)
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
