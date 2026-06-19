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
from app.modules.utility.services.search_utils import like_contains

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

    # Аудит безопасности: sheet_id/gid идут в URL httpx с follow_redirects —
    # валидируем формат, чтобы в URL не попали '/','?','#','@' (ограниченный SSRF).
    import re as _re
    from app.modules.utility.services.gsheets_sync import extract_sheet_id
    sheet_id = extract_sheet_id(sheet_id)  # если вставили полный URL — вытащим ID
    if not _re.fullmatch(r"[A-Za-z0-9_-]+", sheet_id):
        raise HTTPException(status_code=400, detail="Некорректный sheet_id")
    _gid = str(data.gid or settings.GSHEETS_GID or "0")
    if not _re.fullmatch(r"[0-9]+", _gid):
        raise HTTPException(status_code=400, detail="Некорректный gid")

    from app.modules.utility.tasks import sync_gsheets_task

    task = sync_gsheets_task.delay(
        sheet_id=sheet_id,
        gid=_gid,
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


@router.post("/auto-normalize-decimal")
async def trigger_auto_normalize_decimal(
    year: int = Query(2026, ge=2020, le=2100),
    dry_run: bool = Query(True, description="True — preview без изменений"),
    current_user: User = Depends(get_current_user),
):
    """Bug 29.05.2026 (Коммит 18): авто-нормализация пропущенной
    десятичной точки в rejected GSheets-подачах.

    Сценарий: жильцы вводят показания счётчика без десятичной точки.
    Например 1418500 вместо 14185.00. Это `value_too_large` → rejected.
    На проде 77 таких записей за 2026.

    Эвристика: если hot_water или cold_water > MAX_WATER_METER_VALUE
    (99999.999) И /1000 < этого порога — предполагаем что пропущена
    точка после 5-й цифры (стандарт счётчика 5+3). Делим на 1000.

    Пример: 1418500 → 1418.500 (правдоподобно)
            14185000 → 14185.000 (тоже OK)
            141850000 → 141850.000 (НЕ нормализуем — всё равно out of range)

    dry_run=True (default) — показывает что бы изменилось без apply.
    dry_run=False — apply: status auto_approved, conflict_reason=NULL,
    hot_water/cold_water делятся на 1000.

    После apply — запусти /promote-historical для создания reading'ов.
    """
    require_admin(current_user)

    import asyncio
    from app.modules.utility.tasks import sync_db_session
    from datetime import datetime

    def _run():
        with sync_db_session() as db:
            from app.modules.utility.services.reading_validators import (
                MAX_WATER_METER_VALUE,
            )
            from app.modules.utility.models import GSheetsImportRow

            max_water = Decimal(str(MAX_WATER_METER_VALUE))
            start = datetime(year, 1, 1)
            end = datetime(year + 1, 1, 1)

            rows = db.query(GSheetsImportRow).filter(
                GSheetsImportRow.status == "rejected",
                GSheetsImportRow.sheet_timestamp >= start,
                GSheetsImportRow.sheet_timestamp < end,
                GSheetsImportRow.matched_user_id.is_not(None),
            ).all()

            normalized = 0
            skipped = 0
            preview = []

            for r in rows:
                hot = Decimal(str(r.hot_water)) if r.hot_water is not None else None
                cold = Decimal(str(r.cold_water)) if r.cold_water is not None else None

                needs_norm_hot = hot is not None and hot > max_water
                needs_norm_cold = cold is not None and cold > max_water

                if not (needs_norm_hot or needs_norm_cold):
                    skipped += 1
                    continue

                new_hot = hot / Decimal("1000") if needs_norm_hot else hot
                new_cold = cold / Decimal("1000") if needs_norm_cold else cold

                # Проверяем что нормализация дала разумное значение
                if (new_hot is not None and new_hot > max_water) or \
                   (new_cold is not None and new_cold > max_water):
                    skipped += 1
                    continue

                preview.append({
                    "row_id": r.id,
                    "raw_fio": r.raw_fio,
                    "old_hot": float(hot) if hot is not None else None,
                    "new_hot": float(new_hot) if new_hot is not None else None,
                    "old_cold": float(cold) if cold is not None else None,
                    "new_cold": float(new_cold) if new_cold is not None else None,
                })

                if not dry_run:
                    r.hot_water = new_hot
                    r.cold_water = new_cold
                    r.status = "auto_approved"
                    r.conflict_reason = None
                    db.add(r)
                normalized += 1

            if not dry_run and normalized > 0:
                db.commit()

            return {
                "year": year,
                "dry_run": dry_run,
                "total_rejected": len(rows),
                "normalized": normalized,
                "skipped_unsafe": skipped,
                "preview": preview[:20],  # первые 20 для отчёта
            }

    return await asyncio.to_thread(_run)


@router.post("/promote-historical")
async def trigger_promote_historical(
    year: int = Query(..., ge=2020, le=2100),
    current_user: User = Depends(get_current_user),
):
    """Bug 29.05.2026 (Коммит 17): массовый promote auto_approved
    GSheets-подач за весь указанный год.

    Стандартный `promote_auto_approved_rows` работает только для
    АКТИВНОГО периода. Если жилец подал в Январе 2026, status='auto_approved'
    но reading_id=NULL — promote не подберёт его в Мае. Подачи висят.

    Этот endpoint проходит по ВСЕМ BillingPeriod года (по биллинговой
    хронологии), для каждого вызывает promote с фильтром sheet_timestamp
    в соответствующем месяце. На выходе — суммарная статистика.

    Пример: после очистки 29.05.2026 у Калачёва Март 1005, Февраль 999
    и других висели auto_approved. После этого endpoint'а они станут
    GSHEETS_IMPORT reading'ами в соответствующих периодах.
    """
    require_admin(current_user)

    import asyncio
    from app.modules.utility.tasks import sync_db_session
    from app.modules.utility.services.gsheets_sync import promote_auto_approved_rows
    from app.modules.utility.services.period_helpers import period_chron_key

    def _run():
        with sync_db_session() as db:
            # Все периоды этого года, кроме Начального (chron=(0,0))
            all_periods = db.query(BillingPeriod).all()
            year_periods = [
                p for p in all_periods
                if period_chron_key(p.name)[0] == year
            ]
            # Сортируем по хронологии (Февраль → Март → Апрель → Май)
            year_periods.sort(key=lambda p: period_chron_key(p.name))

            summary = {"year": year, "periods_processed": 0, "results": []}
            total_created = 0
            total_skipped = 0
            total_bound = 0
            for period in year_periods:
                result = promote_auto_approved_rows(db, target_period=period)
                summary["results"].append({
                    "period_id": period.id,
                    "period_name": period.name,
                    **result,
                })
                summary["periods_processed"] += 1
                total_created += result.get("created", 0)
                total_skipped += result.get("skipped", 0)
                total_bound += result.get("bound", 0)
            summary["total_created"] = total_created
            summary["total_skipped"] = total_skipped
            summary["total_bound"] = total_bound
            return summary

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
            r.matched_user.room.format_address
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
# Bug AD: ДИАГНОСТИКА РОСТЕРА — admin вставляет копию таблицы из Google
# Sheets, система отвечает «куда какая строка попала».
# Решает вопрос «в логе 56 ФИО, в системе видно только 35 — где остальные».
# =========================================================================
class RosterDiagnoseRequest(BaseModel):
    text: str
    # Если задано — фильтруем поиск reading'ов этим периодом (id). Иначе
    # ищем по всем периодам (полезно, если админ вставляет таблицу за месяц
    # отличный от активного).
    period_id: Optional[int] = None


def _parse_roster_line(line: str) -> Optional[dict]:
    """Парсит одну строку «timestamp ФИО общ комната ГВС ХВС».

    Разделители: таб, многократные пробелы, точка с запятой. Колонок может
    быть от 3 (ФИО + общ + комната) до 6 (полная строка). Возвращает dict
    с полями fio, room, dormitory или None если строка пуста / нечитаема.
    """
    s = (line or "").strip()
    if not s:
        return None

    # Разделители: таб приоритетно, иначе ;, иначе 2+ пробелов
    if "\t" in s:
        parts = [p.strip() for p in s.split("\t") if p.strip()]
    elif ";" in s:
        parts = [p.strip() for p in s.split(";") if p.strip()]
    else:
        # 2+ пробелов как разделитель колонок
        import re as _re
        parts = [p.strip() for p in _re.split(r"\s{2,}", s) if p.strip()]

    if len(parts) < 2:
        return None

    # Первое поле часто timestamp — определяем по наличию '.' и ':'
    ts_first = ("." in parts[0] and ":" in parts[0]) or parts[0][:2].isdigit() and "." in parts[0]
    if ts_first and len(parts) >= 3:
        # timestamp, ФИО, [общ], [комната], [ГВС], [ХВС]
        fio = parts[1]
        dormitory = parts[2] if len(parts) >= 4 else None
        room = parts[3] if len(parts) >= 5 else (parts[2] if len(parts) == 3 else None)
        # Если общежитие выглядит как номер комнаты — переставим
        if dormitory and room and not _looks_like_dorm(dormitory) and _looks_like_room(dormitory):
            room, dormitory = dormitory, None
    else:
        # ФИО, [общ], [комната], [...]
        fio = parts[0]
        dormitory = parts[1] if len(parts) >= 2 else None
        room = parts[2] if len(parts) >= 3 else None
        if dormitory and not _looks_like_dorm(dormitory) and _looks_like_room(dormitory):
            room, dormitory = dormitory, None

    return {"fio": fio, "room": room, "dormitory": dormitory}


def _looks_like_room(v: str) -> bool:
    """Похоже ли значение на номер комнаты (короткий, цифровой/буквенно-цифровой)."""
    if not v:
        return False
    v = v.strip()
    return len(v) <= 6 and any(c.isdigit() for c in v)


def _looks_like_dorm(v: str) -> bool:
    """Похоже ли значение на название общежития (длинное / содержит «стр.»/«дв.»)."""
    if not v:
        return False
    low = v.lower()
    return any(k in low for k in ("стр", "дв.", "общеж", "корп")) or len(v) > 8


@router.post("/diagnose-roster", summary="Сверить список подач с системой")
async def diagnose_roster(
    body: RosterDiagnoseRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Принимает вставленный из Google Sheets текст со списком подач, для
    каждой строки находит:
      - **gsheets_status**: pending / conflict / unmatched / approved /
        auto_approved / rejected / not_in_gsheets (если строки нет в
        gsheets_import_rows вообще);
      - **reading_id**: если status=approved/auto_approved — id MeterReading
        который создал импорт;
      - **user_id / username**: к какому жильцу гет матч;
      - **note**: коротко человеком — что делать дальше.

    Используется со страницы дашборда: «Сверить ростер» → модалка
    с textarea → таблица с раскладкой по каждой строке."""
    require_admin(current_user)

    if not body.text or not body.text.strip():
        raise HTTPException(status_code=400, detail="Пустой текст")

    # Парсим строки
    parsed: list[dict] = []
    for idx, raw_line in enumerate(body.text.splitlines(), start=1):
        rec = _parse_roster_line(raw_line)
        if rec:
            rec["line_no"] = idx
            rec["raw"] = raw_line.strip()
            parsed.append(rec)

    if not parsed:
        return {"items": [], "summary": {"parsed": 0}, "warning": "Не удалось распарсить ни одной строки"}

    # Сводим к уникальным ФИО (если в логе человек подавал несколько раз —
    # достаточно увидеть текущее положение в системе один раз).
    seen_keys = set()
    unique_records: list[dict] = []
    for rec in parsed:
        key = (_normalize_fio(rec["fio"]), (rec.get("room") or "").strip())
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_records.append(rec)

    # Загружаем gsheets_import_rows по нормализованному ФИО.
    # Делаем один SELECT всех нужных rows IN-фильтром по raw_fio (после
    # нормализации). Постом — мапим в Python.
    fios_norm = list({_normalize_fio(r["fio"]) for r in unique_records})

    # Все строки в gsheets_import_rows с подходящим ФИО (берём все статусы).
    gs_rows = (await db.execute(
        select(GSheetsImportRow).options(
            selectinload(GSheetsImportRow.matched_user).selectinload(User.room),
            selectinload(GSheetsImportRow.matched_room),
        ).where(GSheetsImportRow.raw_fio.isnot(None))
    )).scalars().all()

    # Группируем по нормализованному ФИО
    gs_by_fio: dict[str, list[GSheetsImportRow]] = {}
    for r in gs_rows:
        nf = _normalize_fio(r.raw_fio or "")
        if nf in fios_norm:
            gs_by_fio.setdefault(nf, []).append(r)

    # Также ищем напрямую в users (если в gsheets вообще не дошло)
    users_all = (await db.execute(
        select(User).options(selectinload(User.room)).where(
            User.is_deleted.is_(False), User.role == "user"
        )
    )).scalars().all()
    users_by_norm = {}
    for u in users_all:
        nf = _normalize_fio(u.full_name or u.username or "")
        users_by_norm.setdefault(nf, []).append(u)

    # Для approved строк проверяем actual reading
    items: list[dict] = []
    summary = {
        "parsed": len(parsed),
        "unique": len(unique_records),
        "found_reading": 0,
        "in_gsheets_pending": 0,
        "in_gsheets_conflict": 0,
        "in_gsheets_unmatched": 0,
        "in_gsheets_rejected": 0,
        "not_in_gsheets_but_user_exists": 0,
        "user_not_found": 0,
    }

    for rec in unique_records:
        norm = _normalize_fio(rec["fio"])
        gs_list = gs_by_fio.get(norm, [])

        # Берём самую свежую gsheets-строку по этому ФИО.
        # Если в room указано что-то — выбираем строку с совпадением;
        # иначе просто свежую.
        chosen_gs: Optional[GSheetsImportRow] = None
        if gs_list:
            room_target = (rec.get("room") or "").strip()
            if room_target:
                for r in gs_list:
                    if (r.raw_room_number or "").strip() == room_target:
                        chosen_gs = r
                        break
            if chosen_gs is None:
                # Свежая по sheet_timestamp
                chosen_gs = sorted(
                    gs_list,
                    key=lambda x: x.sheet_timestamp or datetime.min,
                    reverse=True,
                )[0]

        status = chosen_gs.status if chosen_gs else "not_in_gsheets"
        user = chosen_gs.matched_user if chosen_gs else None
        room = (chosen_gs.matched_room or (user.room if user else None)) if chosen_gs else None
        reading_id = chosen_gs.reading_id if chosen_gs else None

        # Если не нашли в gsheets — попробуем напрямую User по ФИО
        users_for_fio = users_by_norm.get(norm, [])
        user_exists = len(users_for_fio) > 0

        # note по правилам
        if status == "approved" and reading_id:
            note = "✅ Утверждено, reading создан"
            summary["found_reading"] += 1
        elif status == "auto_approved" and reading_id:
            note = "🤖 Авто-утверждено, reading создан"
            summary["found_reading"] += 1
        elif status == "pending":
            note = "⏳ В матчере: pending"
            summary["in_gsheets_pending"] += 1
        elif status == "conflict":
            note = f"🔀 Конфликт: {chosen_gs.conflict_reason or 'не задано'}"
            summary["in_gsheets_conflict"] += 1
        elif status == "unmatched":
            note = "🔍 Не найден в БД (ФИО не сматчилось)"
            summary["in_gsheets_unmatched"] += 1
        elif status == "rejected":
            note = "🗑 Отклонено админом"
            summary["in_gsheets_rejected"] += 1
        elif status == "not_in_gsheets":
            if user_exists:
                note = "⚠ В gsheets не пришло (sync отстал?), но в БД жилец есть"
                summary["not_in_gsheets_but_user_exists"] += 1
            else:
                note = "❌ Не в gsheets и нет в БД (фио / опечатка)"
                summary["user_not_found"] += 1
        else:
            note = f"? unknown status: {status}"

        items.append({
            "line_no": rec["line_no"],
            "fio": rec["fio"],
            "room_input": rec.get("room"),
            "dormitory_input": rec.get("dormitory"),
            "gsheets_id": chosen_gs.id if chosen_gs else None,
            "gsheets_status": status,
            "gsheets_room": chosen_gs.raw_room_number if chosen_gs else None,
            "user_id": user.id if user else (users_for_fio[0].id if users_for_fio else None),
            "username": user.username if user else (users_for_fio[0].username if users_for_fio else None),
            "matched_room": (
                room.format_address if room else (
                    users_for_fio[0].room.format_address
                    if (users_for_fio and users_for_fio[0].room) else None
                )
            ),
            "reading_id": reading_id,
            "note": note,
        })

    return {"summary": summary, "items": items}


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
                r.matched_user.room.format_address
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

    # housing_001/E2-B: жилец в доме (place_type='house') не подаёт
    # показания. Если админ всё-таки тыкнул «Утвердить» — отдаём 400
    # и предлагаем «Отклонить» или «Переназначить».
    # ВАЖНО: user загружен через `db.get(User, ...)` БЕЗ selectinload,
    # поэтому user.room — это lazy relationship, и в async-режиме его
    # обращение валит `MissingGreenlet` (инцидент 28.05.2026, Вастаев).
    # Подгружаем room явным запросом по user.room_id (это просто column).
    from app.modules.utility.services.room_validators import is_house as _is_house
    if user.room_id:
        _user_room_for_check = await db.get(Room, user.room_id)
        if _user_room_for_check and _is_house(_user_room_for_check):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Жилец «{user.username}» живёт в доме/квартире — "
                    "счётчиков нет, показания не принимаются. "
                    "Используйте «Отклонить» либо «Переназначить» на жильца общежития."
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

    # =====================================================================
    # ЗАЩИТНЫЕ ГВАРДЫ (бэйджу bug «зацикленный конфликт», май 2026):
    # раньше _apply_approve проверял ТОЛЬКО MAX_WATER_METER_VALUE. high_delta,
    # meter_decreased и total_cost_too_high пропускались — reading создавался
    # с гигантским total_cost, потом cleanup_outlier_readings_task (раз в
    # сутки) сбрасывал его в is_approved=False и помечал GSheets-row
    # обратно как conflict с reason='auto_cleanup_data_overflow'. Админ
    # снова жал «Утвердить» — цикл бесконечный.
    #
    # Теперь те же три гварда что в promote_auto_approved_rows: при их
    # срабатывании reading НЕ создаётся, row остаётся в conflict с СВОИМ
    # reason 'manual_approve_blocked: ...'. cleanup_outlier_readings_task
    # такие строки не трогает (он сбрасывает только связанные с outlier
    # reading'ами), и каждое следующее нажатие «Утвердить» снова отдаст
    # 422 с понятной подсказкой — пока админ не выберет правильное
    # действие (make-baseline / swap / fix-decimal / reject).
    # =====================================================================
    from app.modules.utility.services.reading_validators import (
        validate_meter_reading as _validate_mr,
        validate_total_cost as _validate_tc,
    )

    # Готовим prev_* для validate_meter_reading. Три ветки:
    #   1. prev_meaningful есть → обычный prev.
    #   2. prev_meaningful нет, но prev_candidates не пуст → synth-prev
    #      (AUTO_GENERATED 0/0/0, DATA_OVERFLOW_RESET и т.п.). validate_mr
    #      применяет строгий MAX_FIRST_SUBMISSION_VALUE-порог дельты.
    #   3. Истории вообще нет → is_baseline (порог тот же).
    if prev_meaningful is not None:
        _val_prev_hot = prev_meaningful.hot_water or Decimal("0")
        _val_prev_cold = prev_meaningful.cold_water or Decimal("0")
        _val_prev_elect = prev_meaningful.electricity or Decimal("0")
        _prev_is_synth = False
        _is_baseline = False
    elif prev_candidates:
        _synth = prev_candidates[0]
        _val_prev_hot = _synth.hot_water or Decimal("0")
        _val_prev_cold = _synth.cold_water or Decimal("0")
        _val_prev_elect = _synth.electricity or Decimal("0")
        _prev_is_synth = True
        _is_baseline = False
    else:
        _val_prev_hot = _val_prev_cold = _val_prev_elect = None
        _prev_is_synth = False
        _is_baseline = True

    async def _block_with_conflict(reason: str, status_code: int = 422) -> None:
        """Помечает row как conflict с указанным reason и raise'ит HTTPException.

        commit здесь явный — иначе при raise транзакция откатится и UI
        не увидит обновлённый conflict_reason (продолжит показывать старый
        auto_cleanup_data_overflow).
        """
        row.status = "conflict"
        row.conflict_reason = reason
        try:
            await db.commit()
        except Exception:
            await db.rollback()
        raise HTTPException(status_code=status_code, detail=reason)

    # Guard 1: high_delta / baseline overflow.
    _vmr = _validate_mr(
        hot=row.hot_water,
        cold=row.cold_water,
        elect=electricity_value,
        prev_hot=_val_prev_hot,
        prev_cold=_val_prev_cold,
        prev_elect=_val_prev_elect,
        is_baseline=_is_baseline,
        prev_is_synth=_prev_is_synth,
    )
    if not _vmr.ok:
        await _block_with_conflict(
            "manual_approve_blocked: high_delta_or_baseline_overflow: "
            + "; ".join(_vmr.errors)
            + ". Используйте «Сделать baseline» (для первого реального "
            "накопленного показания), «Поменять ГВС/ХВС» (если жилец "
            "перепутал столбцы) или «Отклонить»."
        )

    # Guard 2: meter_decreased — счётчик «упал».
    # Bug 29.05.2026 (Коммит 19): EDGE CASE AUTO-FIX.
    # Если prev_meaningful — это AUTO_NORM (а НЕ реальная manual подача),
    # это значит жилец подал РЕАЛЬНО меньше чем накопленный норматив.
    # По ПП №354 это переплата, а не «упал счётчик». Auto-fix:
    #   1. Найти last_manual (предыдущая РЕАЛЬНАЯ подача).
    #   2. Если current >= last_manual.hot/cold — норматив переоценил.
    #      Удаляем AUTO_NORM-цепочку между last_manual и current, заново
    #      вычисляем breakdown с last_manual как prev.
    #   3. Если current < last_manual — реально упал, оставляем conflict.
    if breakdown and breakdown.get("meter_decreased") and prev_meaningful is not None:
        prev_flags_upper = (prev_meaningful.anomaly_flags or "").upper()
        prev_is_auto_norm = "AUTO_NORM" in prev_flags_upper
        if prev_is_auto_norm:
            # Найти LAST MANUAL (не AUTO_*) для этого жильца строго раньше
            # текущего периода по биллингу.
            from app.modules.utility.services.period_helpers import (
                period_chron_key as _pck,
            )
            target_chron = _pck(target_period.name) if target_period else None
            all_history = (await db.execute(
                select(MeterReading, BillingPeriod)
                .join(BillingPeriod, MeterReading.period_id == BillingPeriod.id)
                .where(
                    MeterReading.user_id == user.id,
                    MeterReading.is_approved.is_(True),
                )
            )).all()
            # Только manual (без AUTO_NORM/AUTO_AVG/AUTO_GENERATED/etc),
            # строго раньше target_chron.
            def _is_manual(r):
                f = (r.anomaly_flags or "").upper()
                for skip in ("AUTO_NORM", "AUTO_AVG", "AUTO_GENERATED",
                             "AUTO_NO_HISTORY", "AUTO_AVG_FALLBACK", "BASELINE"):
                    if skip in f:
                        return False
                return True
            manual_history = [
                (r, p) for r, p in all_history
                if _is_manual(r)
                and (target_chron is None or _pck(p.name) < target_chron)
            ]
            manual_history.sort(key=lambda rp: _pck(rp[1].name), reverse=True)
            last_manual = manual_history[0][0] if manual_history else None

            if (last_manual
                and row.hot_water >= last_manual.hot_water
                and row.cold_water >= last_manual.cold_water):
                # Auto-fix: удалить AUTO_NORM-readings между last_manual и
                # текущим периодом (НЕ трогая current — он ещё не создан).
                last_manual_chron = _pck(
                    next(p for r, p in all_history if r.id == last_manual.id).name
                )
                auto_norm_ids = [
                    r.id for r, p in all_history
                    if "AUTO_NORM" in (r.anomaly_flags or "").upper()
                    and last_manual_chron < _pck(p.name) < target_chron
                ]
                if auto_norm_ids:
                    from sqlalchemy import delete as _sa_delete
                    await db.execute(
                        _sa_delete(MeterReading).where(
                            MeterReading.id.in_(auto_norm_ids)
                        )
                    )
                    await db.flush()
                    import logging as _log_ef
                    _log_ef.getLogger(__name__).info(
                        "[GSHEETS-APPROVE] auto-fix edge case (real<norm): "
                        "user=%s удалено %d AUTO_NORM между last_manual=%s и "
                        "current=%s. Заново вычисляем breakdown.",
                        user.id, len(auto_norm_ids), last_manual.id, target_period,
                    )
                    # Заново вычисляем breakdown с last_manual как prev.
                    breakdown = compute_reading_breakdown(
                        user=user, room=room_obj, tariff=eff_tariff,
                        current_hot=row.hot_water,
                        current_cold=row.cold_water,
                        current_elect=electricity_value,
                        prev_reading=last_manual,
                        heating_season_active=_heating,
                        hot_water_heating_active=_hw,
                    )
                    # prev_meaningful обновляем тоже — для остальных guards
                    prev_meaningful = last_manual
                    # Если новый breakdown больше не meter_decreased — продолжаем.
                    if not breakdown.get("meter_decreased"):
                        # Skip Guard 2 — теперь всё ок.
                        pass
                    else:
                        # Всё ещё meter_decreased даже от last_manual — реально упал.
                        await _block_with_conflict(
                            "manual_approve_blocked: meter_decreased даже после "
                            "auto-fix AUTO_NORM. Реальная подача меньше последней "
                            f"manual id={last_manual.id}. Используйте «Сделать "
                            "baseline» или «Отклонить»."
                        )
                else:
                    # AUTO_NORM не найдены — обычный meter_decreased
                    await _block_with_conflict(
                        "manual_approve_blocked: meter_decreased: счётчик 'упал' — "
                        f"hot {prev_meaningful.hot_water}→{row.hot_water}, "
                        f"cold {prev_meaningful.cold_water}→{row.cold_water}. "
                        "Используйте «Поменять ГВС/ХВС», «Сделать baseline» или «Отклонить»."
                    )
            else:
                # current < last_manual — реально упал.
                await _block_with_conflict(
                    "manual_approve_blocked: meter_decreased: счётчик 'упал' — "
                    f"hot {prev_meaningful.hot_water}→{row.hot_water}, "
                    f"cold {prev_meaningful.cold_water}→{row.cold_water}. "
                    "Возможные причины: смена счётчика без оформления, ошибка ввода "
                    "жильца, или сменился жилец в комнате. Используйте «Поменять "
                    "ГВС/ХВС», «Сделать baseline» или «Отклонить»."
                )
        else:
            # prev_meaningful — реальная manual подача, не AUTO_NORM.
            # Обычный meter_decreased — счётчик реально упал.
            await _block_with_conflict(
                "manual_approve_blocked: meter_decreased: счётчик 'упал' — "
                f"hot {prev_meaningful.hot_water}→{row.hot_water}, "
                f"cold {prev_meaningful.cold_water}→{row.cold_water}. "
                "Возможные причины: смена счётчика без оформления, ошибка ввода "
                "жильца, или сменился жилец в комнате. Используйте «Поменять "
                "ГВС/ХВС», «Сделать baseline» или «Отклонить»."
            )

    # Guard 3: total_cost_too_high — финальный sanity на расчётный итог.
    # Это главная защита от цикла: cleanup_outlier_readings_task ровно по
    # этому критерию сбрасывал reading'и обратно в conflict с тегом
    # auto_cleanup_data_overflow.
    if breakdown:
        _tc = _validate_tc(breakdown.get("total_cost"))
        if not _tc.ok:
            await _block_with_conflict(
                f"manual_approve_blocked: total_cost_too_high: расчётный итог "
                f"{breakdown.get('total_cost')} ₽ превышает санитарный потолок. "
                + "; ".join(_tc.errors)
                + " Используйте «Сделать baseline» (если это первое накопленное "
                "показание счётчика) или «Отклонить»."
            )

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
    # E3-D fix: чистим conflict_reason после успешного approve. Иначе
    # в БД остаётся артефакт прошлой неуспешной попытки (см. Липша
    # id=2296: status='approved' но conflict_reason всё ещё про
    # «hot=794 меньше 805» — это был старый reason до фикса). UI
    # может показывать тёмный «info-tooltip» с устаревшей причиной.
    row.conflict_reason = None

    # RETROACTIVE RECALC ОТКЛЮЧЁН (финал, Коммит 9, 29.05.2026 — revert
    # Коммита 8). История качелей:
    #   * Коммит 3 (56df3bb): добавили сторно volume-cost на auto-цепочке.
    #   * Hotfix 426afe6: отключили — юзер хотел чтобы virtual месяцы
    #     показывали норматив.
    #   * Коммит 8 (f55e2ba): вернули сторно — увидели double-charges и
    #     отрицательные дельты у Капранова.
    #   * Коммит 9 (этот): окончательно отключаем сторно.
    #
    # Почему сторно НЕ нужен (правильное решение):
    #   * auto_fill создаёт AUTO_NORM на virtual с volume = последнее +
    #     норматив и cost_water = норматив × тариф. Жилец видит
    #     «расчёт по нормативу» в каждом пропущенном месяце.
    #   * При manual подаче `compute_reading_breakdown` берёт
    #     `prev_meaningful` = последний reading где is_meaningful_prev=True.
    #     AUTO_NORM НЕ в PREV_SKIP_FLAGS (см. reading_calculator.py:253),
    #     значит он MEANINGFUL.
    #   * Тогда delta для current = current - последний AUTO_NORM
    #     (например 1468 - 1465 = 3), а не от last_manual (1468 - 1456 = 12).
    #   * Cost_water на current = 3 × tariff = ~732 ₽. Сумма cost_water
    #     по всем 4 месяцам = 4 × 732 = 2928 ₽ ≈ реальный расход 12 ×
    #     244 = 2928 ₽. Без сторно, без double-charges.
    #
    # Edge case: если жилец РЕАЛЬНО подал меньше суммарного норматива
    # (current < последний AUTO_NORM), `meter_decreased` сработает и
    # заблокирует подачу. Админ корректирует вручную («Сделать baseline»
    # или «Поменять ГВС/ХВС»).
    #
    # skip_recalc.py остаётся в коде для legacy (старые VOID_VOL reading'и
    # в БД). Окончательно убрать — отдельный cleanup-коммит.

    await write_audit_log(
        db, current_user.id, current_user.username,
        action="gsheets_approve", entity_type="reading", entity_id=reading.id,
        details={"row_id": row.id, "fio": row.raw_fio},
    )
    return reading


@router.post("/rows/{row_id}/swap-columns")
async def swap_row_columns(
    row_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Меняет местами ГВС и ХВС в одной GSheets-строке.

    Use case: жилец в Google-таблице перепутал столбцы (Покидин Фев 2026:
    646/423 вместо 423/646). После swap последовательность подач становится
    монотонной → утверждение обычной кнопкой проходит без conflict.

    Endpoint только меняет значения и сбрасывает conflict_reason. Сам
    MeterReading не создаётся — админ потом жмёт «Утвердить» как обычно.
    """
    require_admin(current_user)
    row = (await db.execute(
        select(GSheetsImportRow)
        .where(GSheetsImportRow.id == row_id)
        .with_for_update()
    )).scalars().first()
    if not row:
        raise HTTPException(404, "Строка не найдена")
    if row.status == "approved" and row.reading_id:
        raise HTTPException(409, "Строка уже утверждена — swap невозможен. Сначала удалите reading.")

    old_hot = row.hot_water
    old_cold = row.cold_water
    row.hot_water = old_cold
    row.cold_water = old_hot
    # Sb conflict_reason если он был только про meter_decreased.
    if row.conflict_reason and ("meter_decreased" in row.conflict_reason or "high_delta" in row.conflict_reason):
        row.conflict_reason = None
    # Возвращаем статус в pending — пусть админ заново оценит после swap.
    if row.status == "conflict":
        row.status = "pending"
    db.add(row)

    try:
        await write_audit_log(
            db, current_user.id, current_user.username,
            action="gsheets_row_swap_columns",
            entity_type="gsheets_import_row",
            entity_id=row.id,
            details={
                "row_id": row.id,
                "fio": row.raw_fio,
                "before": {"hot": str(old_hot), "cold": str(old_cold)},
                "after": {"hot": str(row.hot_water), "cold": str(row.cold_water)},
            },
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("audit_log for swap-columns failed")

    await db.commit()
    return {
        "status": "ok",
        "row_id": row.id,
        "new_hot_water": str(row.hot_water),
        "new_cold_water": str(row.cold_water),
    }


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

    # Уведомление жильцу в переписку QR-портала (если строка сопоставлена) —
    # человек должен узнать, что подача отклонена, и переподать.
    if row.matched_user_id:
        from app.modules.utility.services.qr_portal import notify_reading_rejected
        when = row.sheet_timestamp.strftime("%d.%m.%Y") if row.sheet_timestamp else None
        notify_reading_rejected(db, row.matched_user_id, when)

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

    # resident_type/billing_mode НЕ из payload и НЕ per_capita (2026-06-19):
    # тип выводится из комнаты при заселении; per_capita обнулял счёт холостяка.
    from app.core.auth import get_password_hash
    db_user = User(
        username=data.username.strip(),
        login=data.username.strip(),  # учётка по умолчанию = ФИО, жилец сменит сам
        hashed_password=get_password_hash(data.password),
        role="user",
        workplace=(data.workplace or "").strip() or None,
        residents_count=max(1, int(data.residents_count)),
        room_id=None,  # выставится move_user_to_room
        resident_type="family",
        billing_mode="by_meter",
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
        stmt = stmt.where(User.room.has(Room.room_number.ilike(like_contains(q))))
    elif full_words:
        # AND по полнословным токенам — иначе «иванов петров» вернёт всех
        # у кого ЛИБО фамилия «иванов» ЛИБО имя «петров».
        for fw in full_words:
            stmt = stmt.where(User.username.ilike(like_contains(fw)))
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
                "room": (u.room.format_address if u.room else None),
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
            "room": (user.room.format_address if user.room else None),
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
        room_conds = [Room.room_number.ilike(like_contains(room_clean))]
        if room_digits and room_digits != room_clean:
            room_conds.append(Room.room_number.ilike(like_contains(room_digits)))

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
            "room": (user.room.format_address if user.room else None),
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
            # Формат-подозрение: показание >99999 = потеряна десятичная точка
            # (напр. 775930 вместо 775.930) — частая причина «подмен» в истории.
            "format_suspect": bool(
                (sheet_row.hot_water or 0) > 99999 or (sheet_row.cold_water or 0) > 99999
            ),
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


# =========================================================================
# AUTO-REBUILD FROM GSHEETS — массовая авто-пересборка всех данных за год.
#
# Use case: «Сделать выборку за год → начальный период = самый ранний месяц
# подачи, остальные месяцы → reading'и (latest по timestamp в каждом).
# Применить ко всем жильцам сразу».
#
# Алгоритм:
#   1. Берём все matched GSheetsImportRow за указанный year.
#   2. Группируем по matched_user_id.
#   3. Для каждого жильца:
#      a. Группируем подачи по (year, month) sheet_timestamp;
#      b. В каждом месяце берём latest по sheet_timestamp (если 10 подач
#         в один месяц — побеждает последняя);
#      c. Сортируем picked-список по (year, month) восходяще;
#      d. Первая запись → INITIAL_FROM_GSHEETS (baseline комнаты);
#      e. Остальные → MeterReading per-period через _apply_approve.
#   4. Все «проигравшие» в дедупликации GSheets-строки помечаются
#      status='rejected' с причиной 'superseded_by_later_in_month'.
#   5. Все существующие MeterReading жильца за затронутые периоды
#      удаляются перед созданием новых.
# =========================================================================

def _bucket_by_month(rows: list) -> dict[tuple[int, int], list]:
    """Группирует GSheetsImportRow-список по (year, month) sheet_timestamp."""
    out: dict[tuple[int, int], list] = {}
    for r in rows:
        if not r.sheet_timestamp:
            continue
        key = (r.sheet_timestamp.year, r.sheet_timestamp.month)
        out.setdefault(key, []).append(r)
    return out


# Флаги указывающие что reading создан/правлен админом вручную или
# legacy-патчем. Auto-rebuild такие НЕ должен затирать — это либо данные
# от админа (manual receipt, разовое начисление), либо явно утверждённая
# админом аномалия (ADMIN_APPROVED_OVERFLOW). Источник GSHEETS_*  — это
# НЕ ручная правка, его можно перезаписывать.
_PROTECTED_FLAG_TOKENS = frozenset({
    "MANUAL_RECEIPT",
    "ONE_TIME_CHARGE",
    "ONE_TIME_CHARGE_BASELINE",
    "ADMIN_APPROVED_OVERFLOW",
    "INITIAL_SETUP",  # установлен админом через initial-readings endpoint
    "INITIAL_FROM_FIRST_SUBMISSION",  # установлен админом через «Сделать baseline»
})
_PROTECTED_FLAG_PREFIXES = ("BASELINE_LEGACY",)


def _is_protected_reading(mr) -> bool:
    """True если reading создан админом и не должен быть перезаписан
    auto-rebuild'ом. См. _PROTECTED_FLAG_TOKENS."""
    flags = (mr.anomaly_flags or "").upper()
    if not flags:
        return False
    tokens = [t.strip() for t in flags.replace("|", ",").split(",") if t.strip()]
    for token in tokens:
        if token in _PROTECTED_FLAG_TOKENS:
            return True
        for prefix in _PROTECTED_FLAG_PREFIXES:
            if token.startswith(prefix):
                return True
    return False


def _detect_and_fix_swaps(entries: list[tuple]) -> tuple[Optional[list[tuple]], Optional[list[bool]]]:
    """Детектор перепутанных столбцов ГВС/ХВС.

    entries — список (hot, cold) в хронологическом порядке (baseline + reading'и).
    Возвращает (fixed_entries, swap_mask) с минимальным числом swap'ов чтобы
    последовательность стала монотонно неубывающей по обоим столбцам.
    Если ни одна комбинация не даёт монотонности — возвращает (None, None).

    Алгоритм: brute-force 2^n всех swap-комбинаций. Для n=2..5 это
    4..32 итерации — мгновенно. Выбирается вариант с минимумом swap'ов.

    Кейс Покидина: [(646,423),(428,654),(432,661)] →
      mask=(True,False,False) → [(423,646),(428,654),(432,661)] →
      ГВС 423<428<432 ✓, ХВС 646<654<661 ✓. Swap 1 строки → fix.
    """
    n = len(entries)
    if n <= 1:
        return list(entries), [False] * n

    best_count = None
    best_mask = None
    best_fixed = None

    from itertools import product
    for mask in product([False, True], repeat=n):
        fixed = []
        for i, swap in enumerate(mask):
            h, c = entries[i]
            fixed.append((c, h) if swap else (h, c))
        # Монотонность по обоим столбцам.
        ok = True
        for i in range(1, n):
            if fixed[i][0] < fixed[i - 1][0] or fixed[i][1] < fixed[i - 1][1]:
                ok = False
                break
        if not ok:
            continue
        swap_count = sum(mask)
        if best_count is None or swap_count < best_count:
            best_count = swap_count
            best_mask = list(mask)
            best_fixed = fixed

    if best_mask is None:
        return None, None
    return best_fixed, best_mask


def _validate_user_entry(entry: dict) -> tuple[bool, str]:
    """Sanity-check для одного user-плана. Возвращает (is_ok, skip_reason).

    Защитные правила:
      - baseline.hot/cold ≤ MAX_WATER_METER_VALUE (10000) — иначе у жильца
        пропущена точка (типа Мухаметкулов 850484);
      - монотонность в цепочке reading'ов: каждое следующее ≥ предыдущего
        (иначе после apply будет meter_decreased conflict);
      - все reading'и тоже ≤ MAX_WATER_METER_VALUE.
    """
    from app.modules.utility.services.reading_validators import MAX_WATER_METER_VALUE
    max_v = float(MAX_WATER_METER_VALUE)
    bl = entry["baseline"]
    if bl["hot_water"] > max_v or bl["cold_water"] > max_v:
        return False, (
            f"baseline_too_large: ГВС={bl['hot_water']:.2f} ХВС={bl['cold_water']:.2f} "
            f"> {max_v:.0f}. Похоже на пропущенную десятичную точку — "
            f"исправьте февральскую/мартовскую запись в Google-таблице."
        )
    prev_hot = bl["hot_water"]
    prev_cold = bl["cold_water"]
    for rd in entry["readings"]:
        m_label = f"{rd['year']}-{rd['month']:02d}"
        if rd["hot_water"] > max_v or rd["cold_water"] > max_v:
            return False, (
                f"reading_too_large @ {m_label}: ГВС={rd['hot_water']:.2f} "
                f"ХВС={rd['cold_water']:.2f} > {max_v:.0f}."
            )
        if rd["hot_water"] < prev_hot or rd["cold_water"] < prev_cold:
            return False, (
                f"meter_decreased @ {m_label}: "
                f"ГВС {prev_hot:.2f}→{rd['hot_water']:.2f}, "
                f"ХВС {prev_cold:.2f}→{rd['cold_water']:.2f}. "
                f"Возможно перепутаны столбцы ГВС/ХВС или жилец сменил счётчик."
            )
        prev_hot = rd["hot_water"]
        prev_cold = rd["cold_water"]
    return True, ""


def _build_rebuild_plan(matched_rows: list) -> list[dict]:
    """Возвращает план пересборки: для каждого user_id — baseline + readings.

    matched_rows должны быть отсортированы или содержать matched_user_id.
    План — массив элементов: {user_id, baseline: {...}, readings: [...],
    duplicates: [row_ids проигнорированных как «более ранние в том же месяце»]}.

    Каждый элемент дополнительно содержит is_ok и skip_reason (см.
    _validate_user_entry) — UI/apply используют для исключения проблемных.
    """
    by_user: dict[int, list] = {}
    for r in matched_rows:
        by_user.setdefault(r.matched_user_id, []).append(r)

    plan: list[dict] = []
    for uid, rs in by_user.items():
        # Bug Z-fix2: группируем подачи по сырому raw_room_number из Excel
        # (а НЕ по matched_room_id — он часто = current room жильца
        # потому что fuzzy матчер сматчил всех в текущую). Это раскрывает
        # переезды: Шиян фев=504 / мар-май=212 → две группы.
        # Если raw_room_number отсутствует — fallback на matched_room_id.
        from app.modules.utility.services.gsheets_sync import parse_room_number
        by_room_key: dict[str, list] = {}
        for r in rs:
            key = None
            if r.raw_room_number:
                parsed = parse_room_number(r.raw_room_number)
                if parsed:
                    key = f"raw:{parsed}"
            if key is None and r.matched_room_id is not None:
                key = f"id:{r.matched_room_id}"
            if key is None:
                continue
            by_room_key.setdefault(key, []).append(r)
        if not by_room_key:
            continue
        primary_key = max(by_room_key.keys(), key=lambda k: len(by_room_key[k]))
        primary_rows = by_room_key[primary_key]
        # primary_room_id: пытаемся определить id комнаты (для UI).
        primary_room_id = None
        if primary_key.startswith("id:"):
            primary_room_id = int(primary_key[3:])
        else:
            # raw:NUMBER — берём matched_room_id из любой строки primary_rows
            for pr in primary_rows:
                if pr.matched_room_id is not None:
                    primary_room_id = pr.matched_room_id
                    break
        other_room_row_ids: list[int] = []
        for rk, room_rs in by_room_key.items():
            if rk != primary_key:
                other_room_row_ids.extend(r.id for r in room_rs)

        by_month = _bucket_by_month(primary_rows)
        if not by_month:
            continue
        picked_per_month: dict[tuple[int, int], object] = {}
        duplicates: list[int] = []
        for key, month_rs in by_month.items():
            month_rs_sorted = sorted(month_rs, key=lambda r: r.sheet_timestamp, reverse=True)
            picked_per_month[key] = month_rs_sorted[0]
            duplicates.extend(r.id for r in month_rs_sorted[1:])

        keys_sorted = sorted(picked_per_month.keys())
        if not keys_sorted:
            continue

        baseline_key = keys_sorted[0]
        baseline_row = picked_per_month[baseline_key]
        readings: list[dict] = []
        for key in keys_sorted[1:]:
            row = picked_per_month[key]
            readings.append({
                "year": key[0],
                "month": key[1],
                "row_id": row.id,
                "hot_water": float(row.hot_water or 0),
                "cold_water": float(row.cold_water or 0),
                "sheet_timestamp": row.sheet_timestamp.isoformat() if row.sheet_timestamp else None,
                "current_status": row.status,
            })

        entry = {
            "user_id": uid,
            "primary_room_id": primary_room_id,
            "other_room_row_ids": other_room_row_ids,
            "baseline": {
                "year": baseline_key[0],
                "month": baseline_key[1],
                "row_id": baseline_row.id,
                "hot_water": float(baseline_row.hot_water or 0),
                "cold_water": float(baseline_row.cold_water or 0),
                "sheet_timestamp": baseline_row.sheet_timestamp.isoformat() if baseline_row.sheet_timestamp else None,
            },
            "readings": readings,
            "duplicates_rejected": duplicates,
        }

        # Bug O: попытка авто-детекции перепутанных ГВС/ХВС.
        # Если сейчас монотонность нарушена (meter_decreased), но swap N строк
        # её восстанавливает — фиксируем план применения swap'ов.
        is_ok, skip_reason = _validate_user_entry(entry)
        swaps_to_apply: list[int] = []  # row_ids которые нужно swap'нуть
        if not is_ok and "meter_decreased" in (skip_reason or ""):
            # Собираем хронологическую последовательность.
            chrono = [(entry["baseline"]["hot_water"], entry["baseline"]["cold_water"])]
            chrono += [(r["hot_water"], r["cold_water"]) for r in readings]
            fixed, mask = _detect_and_fix_swaps(chrono)
            if fixed is not None and any(mask):
                # Применяем swap'ы — обновляем значения в entry для UI.
                row_ids_in_order = [entry["baseline"]["row_id"]] + [r["row_id"] for r in readings]
                for i, do_swap in enumerate(mask):
                    if do_swap:
                        swaps_to_apply.append(row_ids_in_order[i])
                # Обновляем baseline и readings новыми значениями.
                new_bh, new_bc = fixed[0]
                entry["baseline"]["hot_water"] = float(new_bh)
                entry["baseline"]["cold_water"] = float(new_bc)
                for idx, r in enumerate(readings):
                    nh, nc = fixed[idx + 1]
                    r["hot_water"] = float(nh)
                    r["cold_water"] = float(nc)
                # Перевалидируем — теперь должно быть OK.
                is_ok, skip_reason = _validate_user_entry(entry)
                entry["swaps_detected"] = True

        entry["is_ok"] = is_ok
        entry["skip_reason"] = skip_reason
        entry["swaps_to_apply"] = swaps_to_apply
        plan.append(entry)
    return plan


@router.get("/auto-rebuild/preview")
async def auto_rebuild_preview(
    year: int = Query(..., ge=2020, le=2100),
    user_id: Optional[int] = Query(None, description="Фильтр: пересборка только для одного жильца"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Показывает план авто-пересборки за год: для каждого жильца — какой
    месяц станет baseline, какие месяцы → reading'и, какие подачи
    «проиграли» дедупликацию (более ранние в том же месяце).

    Если передан user_id — preview/apply только для этого жильца (точечная
    пересборка из карточки в финотчётности)."""
    require_admin(current_user)
    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)

    q = select(GSheetsImportRow).where(
        GSheetsImportRow.sheet_timestamp >= start,
        GSheetsImportRow.sheet_timestamp < end,
        GSheetsImportRow.matched_user_id.is_not(None),
        GSheetsImportRow.status != "rejected",
    )
    if user_id is not None:
        q = q.where(GSheetsImportRow.matched_user_id == user_id)
    rows = (await db.execute(q)).scalars().all()

    # Bug W: фильтр по текущей комнате жильца — ТОЛЬКО для bulk-режима
    # (когда user_id не задан). Для individual rebuild (user_id указан)
    # фильтр снимается, чтобы видеть ВСЕ подачи жильца — админ сам
    # решает что делать с подачами в другой комнате.
    rows_skipped_other_room = 0
    if user_id is None:
        user_room_map = {}
        if rows:
            all_uids = {r.matched_user_id for r in rows if r.matched_user_id}
            if all_uids:
                users_q = (await db.execute(
                    select(User.id, User.room_id).where(User.id.in_(all_uids))
                )).all()
                user_room_map = {uid: rid for uid, rid in users_q}
        rows_filtered = []
        for r in rows:
            cur_room_id = user_room_map.get(r.matched_user_id)
            if cur_room_id is None:
                rows_skipped_other_room += 1
                continue
            if r.matched_room_id and r.matched_room_id != cur_room_id:
                rows_skipped_other_room += 1
                continue
            rows_filtered.append(r)
        rows = rows_filtered

    plan = _build_rebuild_plan(rows)

    # Bug P: для каждого жильца проверяем не лежит ли в БД утв. reading с
    # протектед-флагом (MANUAL_RECEIPT и т.п.) за один из затронутых
    # периодов. Если да — этого жильца НЕ обрабатываем (rebuild стёр бы
    # админскую правку). Сохраняем skip_reason для UI.
    uids = [p["user_id"] for p in plan]
    affected_periods_by_user: dict[int, list[str]] = {}
    for entry in plan:
        period_names = [f"{_MONTH_NAMES_RU[entry['baseline']['month']]} {entry['baseline']['year']}"]
        period_names += [f"{_MONTH_NAMES_RU[r['month']]} {r['year']}" for r in entry["readings"]]
        affected_periods_by_user[entry["user_id"]] = period_names

    if uids:
        all_period_names = {p for names in affected_periods_by_user.values() for p in names}
        if all_period_names:
            period_id_map = {}
            for pname in all_period_names:
                pid = (await db.execute(
                    select(BillingPeriod.id).where(BillingPeriod.name == pname)
                )).scalar_one_or_none()
                if pid:
                    period_id_map[pname] = pid

            for entry in plan:
                if not entry.get("is_ok", True):
                    continue  # уже skipped
                uid = entry["user_id"]
                user_period_ids = [period_id_map.get(p) for p in affected_periods_by_user[uid]]
                user_period_ids = [p for p in user_period_ids if p]
                if not user_period_ids:
                    continue
                existing = (await db.execute(
                    select(MeterReading).where(
                        MeterReading.user_id == uid,
                        MeterReading.is_approved.is_(True),
                        MeterReading.period_id.in_(user_period_ids),
                    )
                )).scalars().all()
                protected = [r for r in existing if _is_protected_reading(r)]
                if protected:
                    entry["is_ok"] = False
                    entry["skip_reason"] = (
                        f"protected_readings: в БД лежит {len(protected)} утв. "
                        f"reading'ов с ручной правкой (флаги: "
                        f"{', '.join(sorted({r.anomaly_flags or '' for r in protected}))}). "
                        f"Rebuild затёр бы их — пропущено."
                    )
                    entry["has_protected"] = True

    # Подгрузим ФИО + адрес для каждого user_id.
    users_q = (await db.execute(
        select(User).options(selectinload(User.room)).where(User.id.in_(uids))
    )).scalars().all() if uids else []
    users_by_id = {u.id: u for u in users_q}
    for entry in plan:
        u = users_by_id.get(entry["user_id"])
        if u:
            entry["username"] = u.username
            entry["full_name"] = u.full_name
            if u.room:
                entry["dormitory_name"] = u.room.dormitory_name
                entry["room_number"] = u.room.room_number

    # Также покажем общую статистику. Раздельно: OK-жильцы (будут обработаны)
    # и SKIPPED (с baseline_too_large / meter_decreased — нужны исправления
    # в GSheets перед apply).
    ok_plan = [p for p in plan if p.get("is_ok", True)]
    skipped_plan = [p for p in plan if not p.get("is_ok", True)]
    swapped_plan = [p for p in ok_plan if p.get("swaps_detected")]
    protected_plan = [p for p in skipped_plan if p.get("has_protected")]
    total_rows = len(rows)

    return {
        "year": year,
        "stats": {
            "total_rows_scanned": total_rows,
            "total_rows_skipped_other_room": rows_skipped_other_room,
            "total_users": len(plan),
            "ok_users": len(ok_plan),
            "skipped_users": len(skipped_plan),
            "swapped_users": len(swapped_plan),
            "protected_users": len(protected_plan),
            "total_baselines": len(ok_plan),
            "total_readings_to_create": sum(len(p["readings"]) for p in ok_plan),
            "total_duplicates_rejected": sum(len(p["duplicates_rejected"]) for p in ok_plan),
        },
        "plan": plan,
    }


@router.post("/auto-rebuild/apply")
async def auto_rebuild_apply(
    year: int = Query(..., ge=2020, le=2100),
    confirm: str = Query(..., description="Должно быть 'YES_REBUILD_FROM_GSHEETS'"),
    user_id: Optional[int] = Query(None, description="Только для одного жильца"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Apply: реализует план из preview.

    Транзакция: одна, либо весь rebuild, либо ничего. Audit log с
    user_ids/counts. Если user_id — пересборка только для одного жильца.
    """
    require_admin(current_user)
    if confirm != "YES_REBUILD_FROM_GSHEETS":
        raise HTTPException(400, "confirm-param должен быть 'YES_REBUILD_FROM_GSHEETS'")

    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    q = select(GSheetsImportRow).where(
        GSheetsImportRow.sheet_timestamp >= start,
        GSheetsImportRow.sheet_timestamp < end,
        GSheetsImportRow.matched_user_id.is_not(None),
        GSheetsImportRow.status != "rejected",
    )
    if user_id is not None:
        q = q.where(GSheetsImportRow.matched_user_id == user_id)
    rows = (await db.execute(q)).scalars().all()

    # Bug W: фильтр по текущей комнате — только для bulk-режима. Для
    # individual rebuild админ должен видеть все подачи жильца.
    if rows and user_id is None:
        all_uids = {r.matched_user_id for r in rows if r.matched_user_id}
        user_room_map = {}
        if all_uids:
            users_q = (await db.execute(
                select(User.id, User.room_id).where(User.id.in_(all_uids))
            )).all()
            user_room_map = {uid: rid for uid, rid in users_q}
        rows = [
            r for r in rows
            if user_room_map.get(r.matched_user_id) is not None
            and (not r.matched_room_id or r.matched_room_id == user_room_map[r.matched_user_id])
        ]

    plan = _build_rebuild_plan(rows)

    # Bug P: повторяем protected-проверку и в apply (preview мог быть давно).
    affected_periods_by_user: dict[int, list[str]] = {}
    for entry in plan:
        names = [f"{_MONTH_NAMES_RU[entry['baseline']['month']]} {entry['baseline']['year']}"]
        names += [f"{_MONTH_NAMES_RU[r['month']]} {r['year']}" for r in entry["readings"]]
        affected_periods_by_user[entry["user_id"]] = names

    all_period_names = {p for names in affected_periods_by_user.values() for p in names}
    if all_period_names:
        period_id_map = {}
        for pname in all_period_names:
            pid = (await db.execute(
                select(BillingPeriod.id).where(BillingPeriod.name == pname)
            )).scalar_one_or_none()
            if pid:
                period_id_map[pname] = pid
        for entry in plan:
            if not entry.get("is_ok", True):
                continue
            uid = entry["user_id"]
            user_period_ids = [pid for pid in (period_id_map.get(p) for p in affected_periods_by_user[uid]) if pid]
            if not user_period_ids:
                continue
            existing = (await db.execute(
                select(MeterReading).where(
                    MeterReading.user_id == uid,
                    MeterReading.is_approved.is_(True),
                    MeterReading.period_id.in_(user_period_ids),
                )
            )).scalars().all()
            if any(_is_protected_reading(r) for r in existing):
                entry["is_ok"] = False
                entry["skip_reason"] = "protected_readings (apply-time)"
                entry["has_protected"] = True

    # Кэш period.id по (year, month) — чтобы не создавать дубли.
    period_cache: dict[tuple[int, int], int] = {}

    async def _get_or_create_period(y: int, m: int) -> BillingPeriod:
        key = (y, m)
        if key in period_cache:
            return await db.get(BillingPeriod, period_cache[key])
        period_name = f"{_MONTH_NAMES_RU[m]} {y}"
        period = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.name == period_name)
        )).scalars().first()
        if not period:
            period = BillingPeriod(name=period_name, is_active=False)
            db.add(period)
            await db.flush()
        period_cache[key] = period.id
        return period

    rows_by_id = {r.id: r for r in rows}

    baselines_created = 0
    readings_created = 0
    readings_deleted = 0
    duplicates_rejected = 0
    errors: list[dict] = []

    skipped_users = 0
    protected_users = 0
    swapped_rows_total = 0
    for entry in plan:
        # Skip жильцов с baseline_too_large / meter_decreased — их данные
        # нужно сначала исправить в Google-таблице. См. _validate_user_entry.
        if not entry.get("is_ok", True):
            skipped_users += 1
            if entry.get("has_protected"):
                protected_users += 1
            continue

        # Bug O: применяем swap ГВС/ХВС в GSheets-строках, которые auto-detector
        # пометил как «перепутаны столбцы». Меняем местами hot_water и cold_water
        # в самой gsheets-строке — далее _apply_approve возьмёт корректные.
        for swap_row_id in entry.get("swaps_to_apply", []):
            swap_row = rows_by_id.get(swap_row_id) or await db.get(GSheetsImportRow, swap_row_id)
            if swap_row:
                old_hot = swap_row.hot_water
                swap_row.hot_water = swap_row.cold_water
                swap_row.cold_water = old_hot
                db.add(swap_row)
                swapped_rows_total += 1
        uid = entry["user_id"]
        user = (await db.execute(
            select(User).options(selectinload(User.room)).where(User.id == uid)
        )).scalars().first()
        if not user or not user.room_id:
            errors.append({"user_id": uid, "error": "Жилец без комнаты"})
            continue
        room = await db.get(Room, user.room_id)
        if not room:
            errors.append({"user_id": uid, "error": "Комната не найдена"})
            continue

        # 1. Удалить существующие MR жильца за затронутые периоды.
        affected_period_names = []
        affected_period_names.append(
            f"{_MONTH_NAMES_RU[entry['baseline']['month']]} {entry['baseline']['year']}"
        )
        for rd in entry["readings"]:
            affected_period_names.append(f"{_MONTH_NAMES_RU[rd['month']]} {rd['year']}")
        affected_periods = (await db.execute(
            select(BillingPeriod.id).where(BillingPeriod.name.in_(affected_period_names))
        )).all()
        affected_period_ids = [r[0] for r in affected_periods]
        if affected_period_ids:
            existing = (await db.execute(
                select(MeterReading.id).where(
                    MeterReading.user_id == uid,
                    MeterReading.period_id.in_(affected_period_ids),
                )
            )).all()
            existing_ids = [r[0] for r in existing]
            if existing_ids:
                from sqlalchemy import update as _upd, delete as _del
                await db.execute(
                    _upd(GSheetsImportRow)
                    .where(GSheetsImportRow.reading_id.in_(existing_ids))
                    .values(reading_id=None, processed_at=None, status="auto_approved")
                )
                await db.execute(_del(MeterReading).where(MeterReading.id.in_(existing_ids)))
                readings_deleted += len(existing_ids)

        # 2. Создать/обновить baseline (INITIAL_FROM_GSHEETS).
        baseline_row = rows_by_id.get(entry["baseline"]["row_id"])
        if baseline_row is None:
            errors.append({"user_id": uid, "error": "baseline_row not found"})
            continue

        new_hot = baseline_row.hot_water or Decimal("0")
        new_cold = baseline_row.cold_water or Decimal("0")

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
        else:
            initial = MeterReading(
                room_id=room.id, user_id=uid, period_id=None,
                hot_water=new_hot, cold_water=new_cold,
                electricity=Decimal("0"),
                is_approved=True,
                anomaly_flags="INITIAL_FROM_GSHEETS",
                anomaly_score=0,
                total_209=Decimal("0"), total_205=Decimal("0"),
            )
            db.add(initial)
            await db.flush()
            initial_id = initial.id

        room.last_hot_water = new_hot
        room.last_cold_water = new_cold
        db.add(room)

        baseline_row.status = "approved"
        baseline_row.reading_id = initial_id
        baseline_row.processed_at = utcnow()
        baseline_row.processed_by_id = current_user.id
        baseline_row.conflict_reason = None
        db.add(baseline_row)
        baselines_created += 1

        # 3. Для каждого remaining-месяца → создаём MeterReading через _apply_approve.
        # _apply_approve сам разруливает period (из sheet_timestamp), считает cost,
        # учитывает meaningful_prev (теперь это INITIAL_FROM_GSHEETS, не AUTO_GENERATED).
        for rd in entry["readings"]:
            r_row = rows_by_id.get(rd["row_id"])
            if r_row is None:
                continue
            r_row.reading_id = None
            r_row.processed_at = None
            r_row.status = "auto_approved"
            db.add(r_row)
            await db.flush()
            try:
                mr = await _apply_approve(db, r_row, current_user)
                r_row.reading_id = mr.id
                r_row.processed_at = utcnow()
                r_row.status = "approved"
                db.add(r_row)
                readings_created += 1
            except HTTPException as e:
                errors.append({
                    "user_id": uid,
                    "row_id": r_row.id,
                    "year": rd["year"], "month": rd["month"],
                    "error": e.detail if hasattr(e, "detail") else str(e),
                })
            except Exception as e:
                errors.append({
                    "user_id": uid, "row_id": r_row.id,
                    "year": rd["year"], "month": rd["month"],
                    "error": str(e),
                })

        # 4. Помечаем дубли (более ранние в том же месяце) как rejected.
        if entry["duplicates_rejected"]:
            from sqlalchemy import update as _upd_d
            await db.execute(
                _upd_d(GSheetsImportRow)
                .where(GSheetsImportRow.id.in_(entry["duplicates_rejected"]))
                .values(
                    status="rejected",
                    conflict_reason="superseded_by_later_in_month",
                    processed_at=utcnow(),
                    processed_by_id=current_user.id,
                )
            )
            duplicates_rejected += len(entry["duplicates_rejected"])

        # Bug AA: коммит после КАЖДОГО успешно обработанного жильца.
        # Без этого общий db.commit() в конце откатывает всё при первой
        # ошибке посреди loop'а — для Шияна вся пересборка терялась.
        try:
            await db.commit()
        except Exception as commit_exc:
            import logging
            logging.getLogger(__name__).warning(
                "[auto-rebuild] commit failed for user_id=%s: %s",
                uid, commit_exc,
            )
            errors.append({
                "user_id": uid,
                "error": f"commit_failed: {commit_exc}",
            })
            await db.rollback()

    # 4.5 ХОЛОСТЯКИ: пересборка считает каждого жильца по ЕГО prev и НЕ тиражирует
    # долю общего счётчика — сосед без истории падает в baseline (был баг:
    # Миронов 389 вместо 1333). После полной пересборки выравниваем доли по
    # затронутым холостяцким комнатам (equalize_singles_room сам берёт источник =
    # макс. счётчик и раскидывает равную долю). 2026-06-18.
    singles_equalized = 0
    try:
        from app.modules.utility.services.singles_billing import equalize_singles_room
        ok_uids = [e["user_id"] for e in plan if e.get("is_ok", True)]
        if ok_uids:
            uid_room = {u: r for u, r in (await db.execute(
                select(User.id, User.room_id).where(User.id.in_(ok_uids))
            )).all()}
            room_ids = {rid for rid in uid_room.values() if rid}
            singles_by_id = {}
            if room_ids:
                singles_by_id = {r.id: r for r in (await db.execute(
                    select(Room).where(Room.id.in_(room_ids), Room.is_singles_apartment.is_(True))
                )).scalars().all()}
            pname_id: dict = {}
            done: set = set()
            for e in plan:
                if not e.get("is_ok", True):
                    continue
                sroom = singles_by_id.get(uid_room.get(e["user_id"]))
                if sroom is None:
                    continue
                for nm in affected_periods_by_user.get(e["user_id"], []):
                    if nm not in pname_id:
                        pname_id[nm] = (await db.execute(
                            select(BillingPeriod.id).where(BillingPeriod.name == nm)
                        )).scalar_one_or_none()
                    pid = pname_id[nm]
                    if pid and (sroom.id, pid) not in done:
                        done.add((sroom.id, pid))
                        try:
                            res = await equalize_singles_room(db, room=sroom, period_id=pid)
                            if res.get("status") == "equalized":
                                singles_equalized += 1
                        except Exception as _ex:  # noqa: BLE001
                            errors.append({"room_id": sroom.id, "period_id": pid,
                                           "error": f"singles_equalize: {str(_ex)[:160]}"})
            if done:
                await db.commit()
    except Exception as _ex:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("[auto-rebuild] singles equalize failed: %s", _ex)

    # 5. Audit log.
    try:
        await write_audit_log(
            db, current_user.id, current_user.username,
            action="auto_rebuild_from_gsheets",
            entity_type="billing_year",
            entity_id=year,
            details={
                "year": year,
                "baselines_created": baselines_created,
                "readings_created": readings_created,
                "readings_deleted": readings_deleted,
                "duplicates_rejected": duplicates_rejected,
                "errors_count": len(errors),
            },
        )
    except Exception:
        import logging
        logging.getLogger(__name__).warning("audit_log for auto-rebuild failed")

    await db.commit()
    return {
        "year": year,
        "baselines_created": baselines_created,
        "readings_created": readings_created,
        "readings_deleted": readings_deleted,
        "duplicates_rejected": duplicates_rejected,
        "skipped_users": skipped_users,
        "protected_users": protected_users,
        "swapped_rows": swapped_rows_total,
        "singles_equalized": singles_equalized,
        "errors_count": len(errors),
        "errors": errors[:50],  # обрезаем чтобы не разнести payload
    }
