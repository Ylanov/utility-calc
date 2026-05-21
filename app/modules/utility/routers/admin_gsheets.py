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
from app.core.time_utils import utcnow
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, or_, desc
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


class ApproveRowBody(BaseModel):
    """Опциональный body для approve_row.

    Используется при конфликтах `value_too_large` — когда жилец ввёл показания
    без десятичной точки (например `96916` вместо `96.916` м³). Админ выбирает
    в UI либо «авто-исправление» (последние 3 цифры → дробная часть, делает
    фронт), либо ручной ввод; в обоих случаях скорректированные значения
    приходят сюда. См. инцидент мая 2026 — 1.48 млрд ₽ на дашборде из-за
    пропущенных точек у нескольких жильцов.
    """
    fix_hot: Optional[Decimal] = None
    fix_cold: Optional[Decimal] = None


class CreateAndMatchRequest(BaseModel):
    """Создать нового жильца и привязать к нему текущую gsheets-row.

    Используется когда fuzzy-матчер не нашёл подходящего жильца И в БД
    его реально нет. Раньше админ должен был уйти во вкладку «Жильцы»,
    вручную создать пользователя, вернуться сюда и переназначить —
    теперь это один POST.
    """
    username: str           # логин для входа в личный кабинет
    password: str           # начальный пароль (потом жилец сможет сменить)
    dormitory_name: str     # «4дв.стр.5» — для поиска комнаты в Жилфонде
    room_number: str        # «104», «101A», и т.п.
    residents_count: int = 1
    resident_type: str = "family"  # family | single
    workplace: Optional[str] = None
    remember: bool = True   # создать GSheetsAlias на это ФИО


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
# SYNC STATUS — поллится фронтом после POST /sync, возвращает прогресс/итог.
# Раньше /sync был fire-and-forget: админ получал task_id и refreshed
# таблицу через 5-15 секунд. Если sync падал или возвращал «unmatched: 1»
# (как в инциденте с Левшиным) — админ ничего не видел.
# Теперь UI может опросить и показать конкретные stats.
# =========================================================================
@router.get("/sync-status/{task_id}")
async def get_sync_status(
    task_id: str,
    current_user: User = Depends(get_current_user),
):
    """Возвращает состояние Celery-таска sync_gsheets_task.

    Состояния:
      PENDING  — задача в очереди, ещё не запущена
      STARTED  — выполняется
      SUCCESS  — закончилась, в `result` — словарь со статистикой
      FAILURE  — упала с исключением, в `result` — текст ошибки
      RETRY    — выполнялась, упала, ушла на retry
      REVOKED  — отменена

    Frontend поллит этот endpoint каждые 2-3 секунды до состояния
    SUCCESS / FAILURE / REVOKED, потом показывает результат тостом.
    """
    require_admin(current_user)

    # Импорт celery лениво — модуль admin_gsheets не должен загружать
    # broker connection просто за счёт регистрации router'а.
    from app.worker import celery as _celery
    res = _celery.AsyncResult(task_id)

    payload: dict = {
        "task_id": task_id,
        "state": res.state,
        "ready": res.ready(),
    }
    if res.state == "SUCCESS":
        # result — это dict, который вернул sync_gsheets_task: inserted/duplicate/
        # unmatched/conflicts/skipped_too_old/auto_approved/matched/promoted_readings.
        payload["result"] = res.result
    elif res.state == "FAILURE":
        # str(exc) — короткая причина для тоста; полный traceback пишется в
        # Celery worker logs, в UI его не показываем.
        payload["error"] = str(res.result) if res.result else "unknown error"
    return payload


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
_MONTH_NAMES_RU = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


async def _resolve_period_for_row(
    db: AsyncSession, row: GSheetsImportRow,
) -> Optional[BillingPeriod]:
    """Возвращает BillingPeriod, к которому привязать создаваемый MeterReading.

    Ранее использовался активный период (одинаковый для всех строк) —
    при импорте исторических подач (например, импорт 34-х подач Неметуллаевой
    за разные месяцы 2025-2026) ВСЕ они пытались встать в текущий
    «Апрель 2026» и конфликтовали duplicate-проверкой после первой.

    Теперь period выводится из sheet_timestamp:
      * найден period с именем «{Месяц} {Год}» — используем его;
      * нет — создаём неактивный (is_active=False), чтобы он не нарушил
        unique-partial-индекс «один активный период»;
      * sheet_timestamp пуст — fallback на текущий активный период
        (старое поведение).

    Это позволяет bulk-approve массово закрывать исторический импорт.
    """
    if not row.sheet_timestamp:
        return (await db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
        )).scalars().first()

    ts = row.sheet_timestamp
    period_name = f"{_MONTH_NAMES_RU[ts.month]} {ts.year}"

    existing = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.name == period_name)
    )).scalars().first()
    if existing:
        return existing

    period = BillingPeriod(name=period_name, is_active=False)
    db.add(period)
    await db.flush()
    return period


async def _apply_approve(
    db: AsyncSession,
    row: GSheetsImportRow,
    current_user: User,
    move_to_raw_room: bool = False,
    fix_hot: Optional[Decimal] = None,
    fix_cold: Optional[Decimal] = None,
) -> MeterReading:
    """
    Создаёт MeterReading на основании импортированной строки.
    Показание сразу помечается как is_approved=True (раз уж админ подтвердил).

    move_to_raw_room=True — если в таблице жилец указал другую комнату (conflict),
    сначала переводим его в новую комнату (User.room_id), а потом создаём
    показание на неё. Используется когда админ решает, что правильная —
    та, что в таблице, а не та, что сейчас у жильца.

    fix_hot / fix_cold — опциональное исправление показаний перед утверждением.
    Используется когда у строки conflict_reason="value_too_large" (жилец
    забыл точку: `96916` вместо `96.916`). Админ в UI выбирает «авто»
    (фронт делит на 1000) или вводит вручную — итог приходит сюда.
    Перевалидируем значения через MAX_WATER_METER_VALUE — если исправление
    всё ещё слишком большое, возвращаем 400 и не утверждаем.
    """
    from app.modules.utility.services.reading_validators import MAX_WATER_METER_VALUE

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

    # Опциональное исправление десятичной точки (см. ApproveRowBody).
    # Делаем ДО проверки hot_water/cold_water is None — на случай если
    # value_too_large обнулил parsed-значения (сейчас не обнуляет, но запас).
    if fix_hot is not None:
        if fix_hot < 0 or fix_hot > MAX_WATER_METER_VALUE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Исправленное значение ГВС {fix_hot} вне допустимого диапазона "
                    f"(0…{MAX_WATER_METER_VALUE}). Проверьте десятичную точку."
                ),
            )
        row.hot_water = fix_hot
    if fix_cold is not None:
        if fix_cold < 0 or fix_cold > MAX_WATER_METER_VALUE:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Исправленное значение ХВС {fix_cold} вне допустимого диапазона "
                    f"(0…{MAX_WATER_METER_VALUE}). Проверьте десятичную точку."
                ),
            )
        row.cold_water = fix_cold
    # После исправления сбрасываем conflict_reason если он был только про value_too_large
    # (т.е. админ исправил то, на что жаловался анализатор).
    if (fix_hot is not None or fix_cold is not None) and row.conflict_reason:
        if "value_too_large" in row.conflict_reason:
            row.conflict_reason = None

    if row.hot_water is None or row.cold_water is None:
        raise HTTPException(
            status_code=400,
            detail="В импортированной строке не разобраны показания ГВС/ХВС",
        )

    # Защита от утверждения «больших» показаний без явного fix-исправления.
    # Раньше bulk-approve мог пропустить row у которой hot_water=96916 (жилец
    # забыл точку) и создать MeterReading со значением > MAX_WATER_METER_VALUE.
    # Это и был механизм инцидента мая 2026 (1.48 млрд ₽ на дашборде).
    # Теперь — если строка помечена value_too_large и админ не передал fix,
    # отказываемся утверждать. Админ должен открыть диалог fix-decimal вручную.
    if (
        (row.hot_water is not None and row.hot_water > MAX_WATER_METER_VALUE)
        or (row.cold_water is not None and row.cold_water > MAX_WATER_METER_VALUE)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Показания превышают допустимый максимум "
                f"({MAX_WATER_METER_VALUE} м³) — вероятно пропущена десятичная точка. "
                "Используйте диалог исправления точки (fix_hot / fix_cold) перед утверждением."
            ),
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

    # Период, к которому привяжем показание.
    # ИСПРАВЛЕНИЕ (apr 2026): раньше брался активный период (одинаковый для
    # всех approvals). Это ломало bulk-approve исторических подач — например,
    # 34 строки Неметуллаевой за разные месяцы 2025-2026 пытались все попасть
    # в текущий «Апрель 2026», и со второй начинался 409 conflict.
    # Теперь period выводится из sheet_timestamp; если его нет — fallback
    # на активный (старое поведение). См. _resolve_period_for_row.
    target_period = await _resolve_period_for_row(db, row)

    # ЗАЩИТА ОТ ДУБЛЕЙ: если в этом периоде у жильца уже есть утверждённое
    # показание, не создаём второе. Отдаём 409 со структурой conflict.
    #
    # ВАЖНО: именно `raise HTTPException(detail=dict)`, а не `return JSONResponse(...)`.
    # Раньше был второй вариант, но _apply_approve тогда возвращал Response
    # вместо MeterReading, и внешний approve_row падал с AttributeError при
    # попытке прочитать .id (и bulk_approve считал такой «возврат» как success).
    # HTTPException корректно всплывает через любой вызывающий код.
    if target_period:
        duplicate = (await db.execute(
            select(MeterReading).where(
                MeterReading.user_id == user.id,
                MeterReading.period_id == target_period.id,
                MeterReading.is_approved.is_(True),
            ).limit(1)
        )).scalars().first()
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        f"У жильца уже есть утверждённое показание за период "
                        f"«{target_period.name}» (id={duplicate.id}). "
                        "Отклоните эту строку или удалите существующее показание."
                    ),
                    "conflict": {
                        "user_username": user.username,
                        "period_name": target_period.name,
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

    # ИСПРАВЛЕНИЕ (may 2026 — «Резунов Р.С.»):
    # Раньше тут создавался MeterReading с total_cost=0, total_209=0, total_205=0
    # БЕЗ вычисления cost_* — то есть жилец подал реальные показания через
    # GSheets, админ нажал «Утвердить» в матчере, reading создавался с
    # правильными hot/cold, но СЧЁТ был нулевой. В финансовой отчётности —
    # «нулевая квитанция», деньги физически не начислялись.
    #
    # Now: используем compute_reading_breakdown (как promote_auto_approved_rows)
    # для расчёта cost_* / total_* через текущий тариф + meaningful prev.
    # Снимаем «Нулевая квитанция»-флаг для approved через matcher.
    from app.modules.utility.services.reading_calculator import (
        compute_reading_breakdown, CalculationError, is_meaningful_prev,
    )
    from app.modules.utility.services.calculations import costs_for_model_fields
    from app.modules.utility.services.tariff_cache import tariff_cache
    from app.modules.utility.routers.settings import _load_seasonal
    # Room уже импортирован в начале файла — повторный локальный импорт
    # затеняет глобальный (F823 на await db.get(Room, ...) выше по функции).

    # Тариф + комната
    room_obj = (await db.execute(
        select(Room).where(Room.id == user.room_id)
    )).scalars().first() if user.room_id else None
    eff_tariff = (
        tariff_cache.get_effective_tariff(user=user, room=room_obj)
        if room_obj else None
    )

    # prev_meaningful — с пропуском synth-флагов
    prev_candidates = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.user_id == user.id,
            MeterReading.is_approved.is_(True),
            MeterReading.period_id < (target_period.id if target_period else 0),
        )
        .order_by(MeterReading.period_id.desc())
        .limit(20)
    )).scalars().all()
    prev_meaningful = next((c for c in prev_candidates if is_meaningful_prev(c)), None)

    breakdown = None
    if eff_tariff is not None:
        try:
            _seasonal = await _load_seasonal(db)
            _heating = _seasonal.heating_season_active and eff_tariff.is_heating_active_now()
            _hw = _seasonal.hot_water_heating_active and eff_tariff.is_hw_heating_active_now()
            breakdown = compute_reading_breakdown(
                user=user, room=room_obj, tariff=eff_tariff,
                current_hot=row.hot_water or Decimal("0"),
                current_cold=row.cold_water or Decimal("0"),
                current_elect=electricity_value,
                prev_reading=prev_meaningful,
                heating_season_active=_heating,
                hot_water_heating_active=_hw,
            )
        except CalculationError:
            breakdown = None

    reading = MeterReading(
        user_id=user.id,
        room_id=user.room_id,
        period_id=target_period.id if target_period else None,
        hot_water=row.hot_water,
        cold_water=row.cold_water,
        electricity=electricity_value,
        is_approved=True,
        anomaly_flags="GSHEETS_IMPORT",
        anomaly_score=0,
        total_cost=breakdown["total_cost"] if breakdown else Decimal("0"),
        total_209=breakdown["total_209"] if breakdown else Decimal("0"),
        total_205=breakdown["total_205"] if breakdown else Decimal("0"),
    )
    if breakdown:
        for k, v in costs_for_model_fields(breakdown).items():
            setattr(reading, k, v)
    db.add(reading)
    await db.flush()

    row.status = "approved"
    row.reading_id = reading.id
    row.processed_at = utcnow()
    row.processed_by_id = current_user.id

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gsheets_approve", entity_type="reading", entity_id=reading.id,
        details={"row_id": row.id, "fio": row.raw_fio},
    )
    return reading


@router.post("/rows/{row_id}/make-baseline")
async def make_row_baseline(
    row_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Превратить GSheets-строку в Начальный период комнаты (baseline).

    Use case: жилец впервые подал реальное накопленное показание счётчика
    (например, Колемагин ГВС 289.565 / ХВС 280.216 — счётчик копит уже
    несколько лет). Валидатор Bug F+G блокирует такую запись как
    high_delta_or_baseline_overflow. Эта кнопка:
      1) ставит значения строки в INITIAL_FROM_GSHEETS-запись комнаты
         (обновляет существующую AUTO_GENERATED 0/0/0 или создаёт новую);
      2) обновляет Room.last_* → следующая подача жильца имеет корректный
         prev и проходит валидацию без conflict;
      3) помечает GSheets-строку approved (но reading_id ссылается на
         initial-reading, чтобы аудит был полный).

    После операции рекомендуется: следующую подачу того же жильца утвердить
    обычной зелёной кнопкой — дельта будет нормальной.
    """
    require_admin(current_user)
    row = (await db.execute(
        select(GSheetsImportRow)
        .where(GSheetsImportRow.id == row_id)
        .with_for_update()
    )).scalars().first()
    if not row:
        raise HTTPException(404, "Строка не найдена")

    if row.status in ("approved", "auto_approved") and row.reading_id:
        raise HTTPException(409, "Эта строка уже утверждена ранее")

    if row.matched_user_id is None:
        raise HTTPException(
            400,
            "Строка не сопоставлена с жильцом — сначала используйте «Назначить жильцу»",
        )

    if row.hot_water is None or row.cold_water is None:
        raise HTTPException(400, "В строке не разобраны показания ГВС/ХВС")

    from app.modules.utility.services.reading_validators import MAX_WATER_METER_VALUE
    if row.hot_water > MAX_WATER_METER_VALUE or row.cold_water > MAX_WATER_METER_VALUE:
        raise HTTPException(
            400,
            f"Показания > {MAX_WATER_METER_VALUE} — вероятно пропущена точка. "
            f"Используйте диалог исправления fix_hot/fix_cold через approve.",
        )

    user = await db.get(User, row.matched_user_id)
    if not user or user.is_deleted:
        raise HTTPException(400, "Жилец не найден или удалён")
    if not user.room_id:
        raise HTTPException(400, "Жилец не привязан к помещению")

    room = await db.get(Room, user.room_id)
    if not room:
        raise HTTPException(400, "Комната жильца не найдена")

    new_hot = row.hot_water
    new_cold = row.cold_water

    # Ищем существующий baseline (INITIAL_SETUP / INITIAL_FROM_FIRST_SUBMISSION /
    # AUTO_GENERATED) — обновляем его значения. Если ничего нет — создаём.
    initial = (await db.execute(
        select(MeterReading).where(
            MeterReading.room_id == room.id,
            MeterReading.anomaly_flags.in_([
                "INITIAL_SETUP",
                "INITIAL_FROM_FIRST_SUBMISSION",
                "INITIAL_FROM_GSHEETS",
                "AUTO_GENERATED",
            ]),
        ).order_by(MeterReading.created_at.desc())
    )).scalars().first()

    if initial is not None:
        initial.hot_water = new_hot
        initial.cold_water = new_cold
        initial.electricity = initial.electricity or Decimal("0")
        initial.anomaly_flags = "INITIAL_FROM_GSHEETS"
        initial.anomaly_score = 0
        initial.is_approved = True
        db.add(initial)
        await db.flush()
        initial_id = initial.id
        initial_action = "updated"
    else:
        initial = MeterReading(
            room_id=room.id,
            user_id=user.id,
            period_id=None,
            hot_water=new_hot,
            cold_water=new_cold,
            electricity=Decimal("0"),
            is_approved=True,
            anomaly_flags="INITIAL_FROM_GSHEETS",
            anomaly_score=0,
            total_209=Decimal("0"),
            total_205=Decimal("0"),
        )
        db.add(initial)
        await db.flush()
        initial_id = initial.id
        initial_action = "created"

    # Обновляем кэш Room — критично для корректной первой дельты.
    room.last_hot_water = new_hot
    room.last_cold_water = new_cold
    db.add(room)

    # Помечаем GSheets-строку approved, привязываем к initial reading.
    row.status = "approved"
    row.reading_id = initial_id
    row.processed_at = utcnow()
    row.processed_by_id = current_user.id
    row.conflict_reason = None
    db.add(row)

    # Audit log.
    try:
        await write_audit_log(
            db, current_user.id, current_user.username,
            action="gsheets_row_make_baseline",
            entity_type="gsheets_import_row",
            entity_id=row.id,
            details={
                "row_id": row.id,
                "fio": row.raw_fio,
                "hot_water": str(new_hot),
                "cold_water": str(new_cold),
                "baseline_reading_id": initial_id,
                "baseline_action": initial_action,
            },
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "audit_log for make-baseline failed for row %s", row.id,
        )

    await db.commit()
    return {
        "status": "ok",
        "baseline_reading_id": initial_id,
        "baseline_action": initial_action,
        "values": {
            "hot_water": str(new_hot),
            "cold_water": str(new_cold),
        },
    }


@router.post("/rows/{row_id}/approve")
async def approve_row(
    row_id: int,
    move_to_raw_room: bool = Query(
        False,
        description="Если True — при conflict-комнат переселяем жильца "
                    "в комнату из таблицы (обновляем User.room_id).",
    ),
    body: Optional[ApproveRowBody] = None,
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
    reading = await _apply_approve(
        db, row, current_user,
        move_to_raw_room=move_to_raw_room,
        fix_hot=body.fix_hot if body else None,
        fix_cold=body.fix_cold if body else None,
    )
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
    row.processed_at = utcnow()
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


# =========================================================================
# CREATE-AND-MATCH — создать нового жильца прямо из gsheets-модалки
# =========================================================================
@router.post("/rows/{row_id}/create-and-match")
async def create_user_and_match(
    row_id: int,
    data: CreateAndMatchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создать нового User + привязать к комнате + привязать gsheets-row.

    Сценарий: gsheets-импорт принёс ФИО которого реально нет в системе.
    Fuzzy-кандидаты дают совпадения 50-60% («Общее отчество»), но это
    другие люди. Reassign не подходит — нужен НОВЫЙ жилец.

    Делаем атомарно:
      1) уникальность username (логина для входа)
      2) ищем комнату по (dormitory_name, room_number) в Жилфонде
      3) создаём User, селим в комнату через move_user_to_room
         (он же открывает RoomAssignment для истории заселений)
      4) привязываем gsheets row + все сестринские unmatched/conflict
         того же ФИО (та же логика что в reassign)
      5) создаём GSheetsAlias если remember=True
    """
    require_admin(current_user)

    row = await db.get(GSheetsImportRow, row_id)
    if not row:
        raise HTTPException(404, "GSheets-row не найдена")
    if row.matched_user_id is not None:
        raise HTTPException(
            400,
            "Подача уже привязана к жильцу — для смены используйте reassign",
        )

    # Username уникален? Сравнение case-insensitive чтобы не было «Ivanov» и «ivanov».
    existing_user = (await db.execute(
        select(User).where(func.lower(User.username) == data.username.strip().lower())
    )).scalars().first()
    if existing_user:
        raise HTTPException(
            400,
            f"Жилец с логином {data.username.strip()!r} уже есть. "
            "Используйте кнопку «Это он» в модалке или поиск.",
        )

    # Комната должна существовать в Жилфонде — мы не создаём её здесь
    # автоматически, чтобы не плодить дубли (admin может неправильно
    # написать название общежития).
    room = (await db.execute(
        select(Room).where(
            Room.dormitory_name == data.dormitory_name.strip(),
            Room.room_number == data.room_number.strip(),
        ).limit(1)
    )).scalars().first()
    if not room:
        raise HTTPException(
            400,
            f"Комната «{data.room_number}» в общежитии «{data.dormitory_name}» "
            "не найдена в Жилфонде. Создайте её сначала во вкладке «Жилфонд», "
            "затем повторите.",
        )

    # Валидация resident_type — иначе через query попадёт мусор в БД.
    rt = data.resident_type if data.resident_type in ("family", "single") else "family"
    bm = "per_capita" if rt == "single" else "by_meter"

    # Создаём User
    from app.core.auth import get_password_hash
    db_user = User(
        username=data.username.strip(),
        hashed_password=get_password_hash(data.password),
        role="user",
        workplace=(data.workplace or "").strip() or None,
        residents_count=max(1, int(data.residents_count)),
        room_id=None,  # выставится move_user_to_room
        resident_type=rt,
        billing_mode=bm,
        is_deleted=False,
        is_initial_setup_done=False,
    )
    db.add(db_user)
    await db.flush()  # нужен db_user.id для room_assignments

    # Селим в комнату (создаст RoomAssignment запись)
    from app.modules.utility.services.room_assignment import move_user_to_room
    await move_user_to_room(
        db, user=db_user, new_room_id=room.id,
        note=f"created via gsheets row #{row_id}",
    )

    # Привязка текущей строки
    row.matched_user_id = db_user.id
    row.matched_room_id = room.id
    row.match_score = 100
    row.status = "pending"
    row.conflict_reason = None

    # Alias чтобы будущие импорты с этим ФИО матчили автоматически
    alias_created = False
    if data.remember:
        alias_created = await _ensure_alias(
            db,
            alias_fio=row.raw_fio,
            user_id=db_user.id,
            kind="manual",
            note="created-and-match",
            created_by_id=current_user.id,
        )

    # Сестринские подачи того же ФИО — та же логика что в reassign,
    # чтобы один create-and-match закрыл всю цепочку.
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
                sr.matched_user_id = db_user.id
                sr.matched_room_id = room.id
                sr.match_score = 100
                sr.status = "pending"
                sr.conflict_reason = None
                siblings_updated += 1

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gsheets_create_and_match", entity_type="user", entity_id=db_user.id,
        details={
            "new_username": data.username.strip(),
            "room_id": room.id,
            "dormitory_name": data.dormitory_name,
            "room_number": data.room_number,
            "gsheets_row_id": row_id,
            "fio": row.raw_fio,
            "alias_created": alias_created,
            "siblings_updated": siblings_updated,
        },
    )
    await db.commit()

    return {
        "status": "ok",
        "user_id": db_user.id,
        "username": data.username.strip(),
        "room_id": room.id,
        "dormitory_name": room.dormitory_name,
        "room_number": room.room_number,
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
    cutoff = utcnow() - timedelta(days=400)
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


# =========================================================================
# HISTORICAL MISMATCHES — диагностика «исторические подмены»
#
# Симптом (инцидент may 2026 «Пегарьков А.В.»): в Google Sheets есть
# строка от 22.03.2023 с показаниями 50/104. В админке у Пегарькова
# апрель 2026 = 50/104 (GSHEETS_AUTO). Reading создан из 2023-строки,
# но в активном периоде 2026 → дельта мая считается от 50 → +111 кубов
# → счёт 73 699 ₽.
#
# Причина: promote_auto_approved_rows раньше не фильтровал sheet_timestamp,
# подхватывал старые «застрявшие» строки. Фикс в gsheets_sync.py делает
# это для НОВЫХ promote, но УЖЕ СОЗДАННЫЕ кривые reading'и админ должен
# разобрать через эту страницу.
# =========================================================================
@router.get("/historical-mismatches")
async def list_historical_mismatches(
    months_threshold: int = Query(2, ge=1, le=24,
        description="Разница в МЕСЯЦАХ между sheet_timestamp и началом периода reading'а"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список reading'ов где связанная gsheets-row имеет sheet_timestamp
    далеко (>=months_threshold месяцев) от начала периода reading'а.

    Это надёжный признак «строку 2023 года импортнули как апрель 2026».
    Сравнение по period.name (формат «Январь 2026»), а не по period.id —
    надёжнее при переименованиях.
    """
    require_admin(current_user)

    # Подгружаем строки GSheets со связанным reading и его period.
    # Фильтруем только processed/auto_approved/approved — pending/conflict
    # ещё не создали reading.
    stmt = (
        select(GSheetsImportRow, MeterReading, BillingPeriod)
        .join(MeterReading, GSheetsImportRow.reading_id == MeterReading.id)
        .join(BillingPeriod, MeterReading.period_id == BillingPeriod.id)
        .options(
            selectinload(GSheetsImportRow.matched_user).selectinload(User.room),
        )
        .where(
            GSheetsImportRow.reading_id.is_not(None),
            GSheetsImportRow.sheet_timestamp.is_not(None),
            GSheetsImportRow.status.in_(["approved", "auto_approved"]),
        )
        .limit(2000)  # читаем с запасом, фильтруем в Python
    )
    rows = (await db.execute(stmt)).all()

    # Период парсим из period.name «Январь 2026» → (year=2026, month=1).
    # Чтобы получить «начало периода». Это даёт стабильное сравнение даже
    # если у периода нет start_date/end_date столбцов.
    _months_ru = {
        "январь": 1, "февраль": 2, "март": 3, "апрель": 4, "май": 5, "июнь": 6,
        "июль": 7, "август": 8, "сентябрь": 9, "октябрь": 10, "ноябрь": 11, "декабрь": 12,
    }
    def _period_start(name: str):
        if not name:
            return None
        parts = name.strip().lower().split()
        if len(parts) != 2:
            return None
        mname, year_s = parts
        m = _months_ru.get(mname)
        if not m:
            return None
        try:
            return datetime(int(year_s), m, 1)
        except (ValueError, TypeError):
            return None

    items = []
    for sheet_row, reading, period in rows:
        period_start = _period_start(period.name)
        if not period_start:
            continue
        # Разница в месяцах (приблизительно — 30 дней/мес).
        delta_days = abs((sheet_row.sheet_timestamp - period_start).days)
        delta_months = delta_days // 30
        if delta_months < months_threshold:
            continue

        user = sheet_row.matched_user
        room = user.room if user else None
        items.append({
            "row_id": sheet_row.id,
            "reading_id": reading.id,
            "user_id": user.id if user else None,
            "username": user.username if user else sheet_row.raw_fio,
            "full_name": getattr(user, "full_name", None) if user else None,
            "dormitory_name": room.dormitory_name if room else sheet_row.raw_dormitory,
            "room_number": room.room_number if room else sheet_row.raw_room_number,
            "sheet_timestamp": sheet_row.sheet_timestamp.isoformat(),
            "reading_period_name": period.name,
            "reading_period_id": period.id,
            "delta_months_approx": delta_months,
            "hot_water": float(sheet_row.hot_water or 0),
            "cold_water": float(sheet_row.cold_water or 0),
            "reading_is_approved": bool(reading.is_approved),
            "reading_total_cost": float(reading.total_cost or 0),
        })

    # Сортируем: сначала с самой большой дельтой (самые «старые подделки»).
    items.sort(key=lambda x: -x["delta_months_approx"])
    items = items[:limit]

    return {
        "threshold_months": months_threshold,
        "count": len(items),
        "items": items,
    }


# =========================================================================
# RELOAD PERIOD FROM GSHEETS — полная переподгрузка периода из таблицы.
#
# Use case: апрель 2026 был сломан (исторические подмены, race condition,
# pre-fix promote). В БД остался «грязный» снимок. Админ хочет: «удали
# всё за апрель и подгрузи заново из Google-таблицы, по факту».
#
# Двухстадийный workflow: preview → admin визуально проверяет diff →
# apply. Не один POST с моментальной заменой — потому что это деньги
# на квитанциях, ошибка тут стоит дорого.
# =========================================================================

def _month_window(year: int, month: int) -> tuple[datetime, datetime]:
    """[start, end) для месяца в локальной TZ. Используется для диапазонов
    sheet_timestamp."""
    start = datetime(year, month, 1)
    if month == 12:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 1, 1)
    return start, end


def _pick_primary_row(rows: list) -> Optional[object]:
    """Самая свежая строка по sheet_timestamp (или created_at в fallback)."""
    if not rows:
        return None
    return max(
        rows,
        key=lambda r: (r.sheet_timestamp or r.created_at or datetime.min),
    )


@router.get("/reload-period/preview")
async def reload_period_preview(
    year: int = Query(..., ge=2020, le=2100),
    month: int = Query(..., ge=1, le=12),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_admin(current_user)
    """Что произойдёт при reload-period: список текущих MR (будет удалено)
    и список gsheets-строк за этот месяц (будет создано вместо них).

    Дедупликация: для каждого user_id берём САМУЮ СВЕЖУЮ строку GSheets
    внутри окна (по sheet_timestamp) — она победит, остальные не создают
    отдельных reading'ов.

    Сравнение по user_id, не по reading_id — потому что после reload
    конкретные ID не сохраняются, важен только итоговый снимок.
    """
    if not (1 <= month <= 12):
        raise HTTPException(400, "month должен быть 1..12")

    period_name = f"{_MONTH_NAMES_RU[month]} {year}"
    period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.name == period_name)
    )).scalars().first()

    # Текущие reading'и за период.
    current_readings = []
    if period is not None:
        from sqlalchemy.orm import selectinload as _sel
        rs = (await db.execute(
            select(MeterReading)
            .options(_sel(MeterReading.user).selectinload(User.room))
            .where(MeterReading.period_id == period.id)
            .order_by(MeterReading.id.desc())
        )).scalars().all()
        for r in rs:
            u = r.user
            room = u.room if u else None
            current_readings.append({
                "reading_id": r.id,
                "user_id": u.id if u else None,
                "username": u.username if u else None,
                "full_name": u.full_name if u else None,
                "dormitory_name": room.dormitory_name if room else None,
                "room_number": room.room_number if room else None,
                "hot_water": float(r.hot_water or 0),
                "cold_water": float(r.cold_water or 0),
                "electricity": float(r.electricity or 0),
                "total_cost": float(r.total_cost or 0),
                "is_approved": bool(r.is_approved),
                "anomaly_flags": r.anomaly_flags,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            })

    # GSheets-строки в окне месяца. Берём ВСЕ status'ы кроме rejected,
    # потому что после reload мы хотим переутвердить даже unmatched/conflict
    # если они matched_user_id-укомплектованы (а unmatched исключаем, у них
    # user_id=NULL — создать reading нельзя).
    start, end = _month_window(year, month)
    all_rows = (await db.execute(
        select(GSheetsImportRow)
        .where(
            GSheetsImportRow.sheet_timestamp >= start,
            GSheetsImportRow.sheet_timestamp < end,
            GSheetsImportRow.matched_user_id.is_not(None),
            GSheetsImportRow.status != "rejected",
        )
        .order_by(desc(GSheetsImportRow.sheet_timestamp))
    )).scalars().all()

    # Группируем по matched_user_id, для каждого user берём primary (самую
    # свежую строку).
    by_user: dict[int, list] = {}
    for r in all_rows:
        by_user.setdefault(r.matched_user_id, []).append(r)

    gsheets_picked = []
    for uid, rows in by_user.items():
        primary = _pick_primary_row(rows)
        if primary is None:
            continue
        # Подтянем user/room для отображения.
        u = (await db.execute(
            select(User).options(selectinload(User.room)).where(User.id == uid)
        )).scalars().first()
        room = u.room if u else None
        gsheets_picked.append({
            "row_id": primary.id,
            "row_ids_all": [r.id for r in rows],
            "user_id": uid,
            "username": u.username if u else None,
            "full_name": u.full_name if u else None,
            "dormitory_name": room.dormitory_name if room else None,
            "room_number": room.room_number if room else None,
            "hot_water": float(primary.hot_water or 0),
            "cold_water": float(primary.cold_water or 0),
            "sheet_timestamp": primary.sheet_timestamp.isoformat() if primary.sheet_timestamp else None,
            "match_score": int(primary.match_score or 0),
            "status": primary.status,
            "conflict_reason": primary.conflict_reason,
            "duplicate_rows_in_month": len(rows),
        })

    # Diff по user_id.
    current_by_user = {r["user_id"]: r for r in current_readings if r["user_id"]}
    gsheets_by_user = {g["user_id"]: g for g in gsheets_picked}
    will_delete = []   # есть в БД, нет в GSheets за этот месяц
    will_replace = []  # есть в обоих — значения изменятся
    will_create = []   # нет в БД, есть в GSheets
    for uid, cur in current_by_user.items():
        if uid in gsheets_by_user:
            g = gsheets_by_user[uid]
            diff = {
                "hot_water_changed": abs(g["hot_water"] - cur["hot_water"]) > 0.001,
                "cold_water_changed": abs(g["cold_water"] - cur["cold_water"]) > 0.001,
            }
            will_replace.append({**cur, "new_hot_water": g["hot_water"], "new_cold_water": g["cold_water"], **diff})
        else:
            will_delete.append(cur)
    for uid, g in gsheets_by_user.items():
        if uid not in current_by_user:
            will_create.append(g)

    return {
        "period_name": period_name,
        "period_exists": period is not None,
        "period_id": period.id if period else None,
        "current_count": len(current_readings),
        "gsheets_count": len(gsheets_picked),
        "duplicate_rows_total": len(all_rows) - len(gsheets_picked),
        "diff": {
            "to_delete": will_delete,
            "to_replace": will_replace,
            "to_create": will_create,
        },
    }


@router.post("/reload-period/apply")
async def reload_period_apply(
    year: int = Query(..., ge=2020, le=2100),
    month: int = Query(..., ge=1, le=12),
    confirm: str = Query(..., description="Должно быть 'YES_DELETE_AND_RELOAD'"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_admin(current_user)
    """Удаляет все MeterReading за период {Месяц} {Год} и создаёт новые из
    GSheets-строк с sheet_timestamp в этом окне.

    Защита: confirm-param должен быть строго 'YES_DELETE_AND_RELOAD' —
    защита от случайного POST'а (например, из автотестов или curl).

    Транзакция: одна — либо весь reload, либо ни одного изменения.
    Audit log: одна запись reload_period с counts + список затронутых user_id.
    """
    if confirm != "YES_DELETE_AND_RELOAD":
        raise HTTPException(
            status_code=400,
            detail="confirm-param должен быть 'YES_DELETE_AND_RELOAD'",
        )
    if not (1 <= month <= 12):
        raise HTTPException(400, "month должен быть 1..12")

    period_name = f"{_MONTH_NAMES_RU[month]} {year}"
    period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.name == period_name)
    )).scalars().first()

    deleted_user_ids: list[int] = []
    deleted_count = 0

    # 1. Удалить все MR за период.
    if period is not None:
        from app.modules.utility.models import GSheetsImportRow as _GR, MeterReading as _MR
        from sqlalchemy import update as _upd, delete as _del

        existing_readings = (await db.execute(
            select(_MR.id, _MR.user_id).where(_MR.period_id == period.id)
        )).all()
        existing_ids = [row[0] for row in existing_readings]
        deleted_user_ids = [row[1] for row in existing_readings if row[1]]
        deleted_count = len(existing_ids)

        if existing_ids:
            # Отвязать gsheets-строки.
            await db.execute(
                _upd(_GR)
                .where(_GR.reading_id.in_(existing_ids))
                .values(reading_id=None, processed_at=None, status="auto_approved")
            )
            # Удалить reading'и.
            await db.execute(_del(_MR).where(_MR.id.in_(existing_ids)))

    # 2. Получить gsheets-строки за месяц, дедуплицировать.
    start, end = _month_window(year, month)
    all_rows = (await db.execute(
        select(GSheetsImportRow)
        .where(
            GSheetsImportRow.sheet_timestamp >= start,
            GSheetsImportRow.sheet_timestamp < end,
            GSheetsImportRow.matched_user_id.is_not(None),
            GSheetsImportRow.status != "rejected",
        )
        .order_by(desc(GSheetsImportRow.sheet_timestamp))
    )).scalars().all()

    by_user: dict[int, list] = {}
    for r in all_rows:
        by_user.setdefault(r.matched_user_id, []).append(r)

    # 3. Для каждой primary-строки → _apply_approve (она сама создаст
    # period или возьмёт существующий из sheet_timestamp).
    created = 0
    errors: list[dict] = []
    for uid, rows in by_user.items():
        primary = _pick_primary_row(rows)
        if primary is None:
            continue
        # Сбрасываем reading_id перед approve.
        primary.reading_id = None
        primary.processed_at = None
        primary.status = "auto_approved"
        db.add(primary)
        await db.flush()
        try:
            mr = await _apply_approve(db, primary, current_user)
            primary.reading_id = mr.id
            primary.processed_at = utcnow()
            primary.status = "approved"
            db.add(primary)
            created += 1
        except HTTPException as e:
            errors.append({
                "row_id": primary.id,
                "user_id": uid,
                "error": e.detail if hasattr(e, "detail") else str(e),
            })
        except Exception as e:
            errors.append({
                "row_id": primary.id,
                "user_id": uid,
                "error": str(e),
            })

    # 4. Audit log.
    from app.modules.utility.routers.admin_dashboard import write_audit_log
    try:
        await write_audit_log(
            db, current_user.id, current_user.username,
            action="reload_period_from_gsheets",
            entity_type="billing_period",
            entity_id=period.id if period else 0,
            details={
                "period_name": period_name,
                "year": year,
                "month": month,
                "deleted_count": deleted_count,
                "created_count": created,
                "errors_count": len(errors),
                "deleted_user_ids_sample": deleted_user_ids[:50],
            },
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "audit_log for reload_period failed: %s", exc,
        )

    await db.commit()
    return {
        "period_name": period_name,
        "deleted_count": deleted_count,
        "created_count": created,
        "errors_count": len(errors),
        "errors": errors,
    }
