import os
import uuid
import asyncio
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, or_, desc, asc
from app.core.database import get_db
# Добавлен импорт модели Room
from app.modules.utility.models import User, MeterReading, BillingPeriod, Room, DebtImportLog
from app.core.dependencies import get_current_user
from app.modules.utility.schemas import PaginatedResponse, UserDebtResponse
from app.modules.utility.tasks import import_debts_task

router = APIRouter(prefix="/api/financier", tags=["Financier"])
logger = logging.getLogger(__name__)

TEMP_DIR = "/app/static/temp_imports"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
os.makedirs(TEMP_DIR, exist_ok=True)


@router.post("/import-debts", summary="Фоновый импорт долгов из 1С")
async def upload_debts_1c(
        account_type: str = Form(..., pattern="^(209|205)$", description="Тип счета: 209 или 205"),
        file: UploadFile = File(...),
        current_user: User = Depends(get_current_user)
):
    if current_user.role not in ("financier", "accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Поддерживаются только Excel-файлы")

    header = await file.read(8)
    await file.seek(0)
    if not (header.startswith(b"PK\x03\x04") or header.startswith(b"\xd0\xcf\x11\xe0")):
        raise HTTPException(status_code=400, detail="Вредоносный файл или поддельное расширение!")

    file.file.seek(0, 2)
    file_size = file.file.tell()
    await file.seek(0)

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"Файл слишком большой. Максимум {MAX_FILE_SIZE / 1024 / 1024} MB"
        )

    ext = file.filename.split(".")[-1]
    unique_name = f"{uuid.uuid4()}.{ext}"
    file_path = os.path.join(TEMP_DIR, unique_name)

    try:
        content = await file.read()

        def save_file():
            with open(file_path, "wb") as buffer:
                buffer.write(content)

        await asyncio.to_thread(save_file)

    except Exception:
        logger.exception("Failed to save uploaded file")
        raise HTTPException(status_code=500, detail="Ошибка сохранения файла")

    task = import_debts_task.delay(
        file_path, account_type,
        started_by_id=current_user.id,
        started_by_username=current_user.username,
    )
    logger.info(f"[IMPORT] Started task={task.id} for account={account_type}")

    return {
        "task_id": task.id,
        "status": "processing",
        "account_type": account_type
    }


@router.get(
    "/users-status",
    response_model=PaginatedResponse[UserDebtResponse],
    summary="Список пользователей с долгами"
)
async def get_users_with_debts(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=500),
        search: str | None = Query(None),
        # Новые фильтры/сортировка для вкладки «Долги 1С»
        only_debtors: bool = Query(False, description="Только с положительным долгом 209 или 205"),
        only_overpaid: bool = Query(False, description="Только с положительной переплатой"),
        dormitory: Optional[str] = Query(None, description="Фильтр по названию общежития"),
        min_debt: Optional[float] = Query(None, ge=0, description="Минимальный суммарный долг (209+205)"),
        sort_by: str = Query("room", pattern="^(room|username|debt|overpay|total)$"),
        sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("financier", "accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    offset = (page - 1) * limit

    res_period = await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )
    active_period = res_period.scalars().first()
    period_id = active_period.id if active_period else None

    d209 = func.coalesce(func.sum(MeterReading.debt_209), 0).label("debt_209")
    o209 = func.coalesce(func.sum(MeterReading.overpayment_209), 0).label("overpayment_209")
    d205 = func.coalesce(func.sum(MeterReading.debt_205), 0).label("debt_205")
    o205 = func.coalesce(func.sum(MeterReading.overpayment_205), 0).label("overpayment_205")
    total = func.coalesce(func.sum(MeterReading.total_cost), 0).label("current_total_cost")

    stmt = select(
        User, Room, d209, o209, d205, o205, total,
    ).outerjoin(
        Room, User.room_id == Room.id
    ).outerjoin(
        MeterReading,
        (User.id == MeterReading.user_id) &
        (MeterReading.period_id == period_id)
    ).where(
        User.is_deleted.is_(False),
        User.role == "user",
    )

    search_condition = None
    if search:
        search_value = f"%{search.lower()}%"
        search_condition = or_(
            func.lower(User.username).like(search_value),
            func.lower(Room.dormitory_name).like(search_value),
            func.lower(Room.room_number).like(search_value)
        )
        stmt = stmt.where(search_condition)

    if dormitory:
        stmt = stmt.where(Room.dormitory_name == dormitory)

    stmt = stmt.group_by(User.id, Room.id)

    # Фильтры по агрегированным значениям — HAVING
    if only_debtors:
        stmt = stmt.having((d209 + d205) > 0)
    if only_overpaid:
        stmt = stmt.having((o209 + o205) > 0)
    if min_debt is not None:
        stmt = stmt.having((d209 + d205) >= min_debt)

    # Сортировка: столбец + направление
    sort_map = {
        "room": (Room.dormitory_name, Room.room_number),
        "username": (User.username,),
        "debt": ((d209 + d205).label("__debt_sum"),),
        "overpay": ((o209 + o205).label("__over_sum"),),
        "total": (total,),
    }
    cols = sort_map[sort_by]
    direction = desc if sort_dir == "desc" else asc
    order_cols = [direction(c).nulls_last() for c in cols]
    # Стабилизатор — вторичная сортировка по username
    if sort_by != "username":
        order_cols.append(asc(User.username))
    stmt = stmt.order_by(*order_cols).limit(limit).offset(offset)

    # count
    count_stmt = select(func.count(User.id)).outerjoin(Room, User.room_id == Room.id).where(
        User.is_deleted.is_(False), User.role == "user"
    )
    if search_condition is not None:
        count_stmt = count_stmt.where(search_condition)
    if dormitory:
        count_stmt = count_stmt.where(Room.dormitory_name == dormitory)

    # Для only_debtors/only_overpaid/min_debt count тоже надо пересчитать через HAVING —
    # делаем через subquery вместо дублирования логики.
    if only_debtors or only_overpaid or min_debt is not None:
        inner = stmt.with_only_columns(User.id).limit(None).offset(None).order_by(None).subquery()
        count_stmt = select(func.count()).select_from(inner)

    total_res = await db.execute(count_stmt)
    total_items = total_res.scalar_one()

    result = await db.execute(stmt)
    rows = result.all()

    items = []
    for row in rows:
        user_obj, room_obj = row[0], row[1]
        items.append({
            "id": user_obj.id,
            "username": user_obj.username,
            "room": room_obj,
            "debt_209": row[2],
            "overpayment_209": row[3],
            "debt_205": row[4],
            "overpayment_205": row[5],
            "current_total_cost": row[6]
        })

    return {"total": total_items, "page": page, "size": limit, "items": items}


# =========================================================================
# NEW: KPI / STATS / EXPORT / HISTORY / UNDO / RECONCILE
# =========================================================================

def _require_finance(user: User) -> None:
    if user.role not in ("financier", "accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")


@router.get("/debts/stats", summary="KPI по долгам (активный период)")
async def debts_stats(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сводка долгов для шапки вкладки «Долги 1С»."""
    _require_finance(current_user)

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    period_id = active_period.id if active_period else None

    # Агрегация по всем readings активного периода
    agg_q = select(
        func.coalesce(func.sum(MeterReading.debt_209), 0),
        func.coalesce(func.sum(MeterReading.overpayment_209), 0),
        func.coalesce(func.sum(MeterReading.debt_205), 0),
        func.coalesce(func.sum(MeterReading.overpayment_205), 0),
        func.count(MeterReading.id),
    ).where(MeterReading.period_id == period_id)
    agg = (await db.execute(agg_q)).one()
    total_debt_209, total_over_209, total_debt_205, total_over_205, readings_count = agg

    # Должников: жильцов где сумма debt_209+205 > 0 в активном периоде
    debtors_q = (
        select(func.count(func.distinct(MeterReading.user_id)))
        .where(
            MeterReading.period_id == period_id,
            (MeterReading.debt_209 > 0) | (MeterReading.debt_205 > 0),
        )
    )
    debtors_count = (await db.execute(debtors_q)).scalar_one()

    # Переплатчиков
    overpayers_q = (
        select(func.count(func.distinct(MeterReading.user_id)))
        .where(
            MeterReading.period_id == period_id,
            (MeterReading.overpayment_209 > 0) | (MeterReading.overpayment_205 > 0),
        )
    )
    overpayers_count = (await db.execute(overpayers_q)).scalar_one()

    # Всего активных жильцов
    total_users_q = select(func.count(User.id)).where(
        User.is_deleted.is_(False), User.role == "user",
    )
    total_users = (await db.execute(total_users_q)).scalar_one()

    total_debt = float(total_debt_209 or 0) + float(total_debt_205 or 0)
    total_over = float(total_over_209 or 0) + float(total_over_205 or 0)
    avg_debt = (total_debt / debtors_count) if debtors_count else 0.0

    # Последний импорт
    last_log = (await db.execute(
        select(DebtImportLog).order_by(desc(DebtImportLog.started_at)).limit(1)
    )).scalars().first()

    # Распределение по общежитиям: ТОП-10 по долгу
    by_dorm_q = (
        select(
            Room.dormitory_name,
            func.sum(MeterReading.debt_209 + MeterReading.debt_205).label("total_debt"),
            func.count(func.distinct(MeterReading.user_id)).label("debtors"),
        )
        .select_from(MeterReading)
        .join(Room, Room.id == MeterReading.room_id)
        .where(
            MeterReading.period_id == period_id,
            (MeterReading.debt_209 > 0) | (MeterReading.debt_205 > 0),
        )
        .group_by(Room.dormitory_name)
        .order_by(desc("total_debt"))
        .limit(10)
    )
    by_dorm = [
        {"name": r[0] or "—", "total_debt": float(r[1] or 0), "debtors": int(r[2] or 0)}
        for r in (await db.execute(by_dorm_q)).all()
    ]

    return {
        "period_name": active_period.name if active_period else None,
        "period_id": period_id,
        "total_users": total_users,
        "debtors_count": debtors_count,
        "overpayers_count": overpayers_count,
        "total_debt_209": float(total_debt_209 or 0),
        "total_debt_205": float(total_debt_205 or 0),
        "total_debt": round(total_debt, 2),
        "total_overpay_209": float(total_over_209 or 0),
        "total_overpay_205": float(total_over_205 or 0),
        "total_overpay": round(total_over, 2),
        "avg_debt_per_debtor": round(avg_debt, 2),
        "readings_count": int(readings_count or 0),
        "last_import": {
            "id": last_log.id,
            "account_type": last_log.account_type,
            "status": last_log.status,
            "started_at": last_log.started_at.isoformat() if last_log.started_at else None,
            "started_by": last_log.started_by_username,
            "updated": last_log.updated,
            "created": last_log.created,
            "not_found_count": last_log.not_found_count,
        } if last_log else None,
        "by_dormitory": by_dorm,
    }


@router.get("/debts/dormitories", summary="Список общежитий для фильтра")
async def debts_dormitories(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    rows = (await db.execute(
        select(Room.dormitory_name).distinct().order_by(Room.dormitory_name)
    )).scalars().all()
    return [r for r in rows if r]


@router.get("/debts/export", summary="Excel-выгрузка текущего списка долгов")
async def debts_export(
    search: str | None = Query(None),
    only_debtors: bool = Query(False),
    only_overpaid: bool = Query(False),
    dormitory: Optional[str] = Query(None),
    min_debt: Optional[float] = Query(None, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Excel-файл с теми же фильтрами, что и в таблице UI.
    Без пагинации — выгружает все подходящие записи.
    """
    _require_finance(current_user)

    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    period_id = active_period.id if active_period else None

    d209 = func.coalesce(func.sum(MeterReading.debt_209), 0).label("debt_209")
    o209 = func.coalesce(func.sum(MeterReading.overpayment_209), 0).label("overpayment_209")
    d205 = func.coalesce(func.sum(MeterReading.debt_205), 0).label("debt_205")
    o205 = func.coalesce(func.sum(MeterReading.overpayment_205), 0).label("overpayment_205")
    tot = func.coalesce(func.sum(MeterReading.total_cost), 0).label("total_cost")

    stmt = select(User, Room, d209, o209, d205, o205, tot).outerjoin(
        Room, User.room_id == Room.id
    ).outerjoin(
        MeterReading,
        (User.id == MeterReading.user_id) & (MeterReading.period_id == period_id)
    ).where(User.is_deleted.is_(False), User.role == "user")

    if search:
        sv = f"%{search.lower()}%"
        stmt = stmt.where(or_(
            func.lower(User.username).like(sv),
            func.lower(Room.dormitory_name).like(sv),
            func.lower(Room.room_number).like(sv),
        ))
    if dormitory:
        stmt = stmt.where(Room.dormitory_name == dormitory)
    stmt = stmt.group_by(User.id, Room.id)
    if only_debtors:
        stmt = stmt.having((d209 + d205) > 0)
    if only_overpaid:
        stmt = stmt.having((o209 + o205) > 0)
    if min_debt is not None:
        stmt = stmt.having((d209 + d205) >= min_debt)

    stmt = stmt.order_by(Room.dormitory_name.asc().nulls_last(),
                         Room.room_number.asc().nulls_last(),
                         User.username.asc())
    rows = (await db.execute(stmt)).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Долги 1С"
    headers = ["ID", "ФИО", "Общежитие", "Комната", "Долг 209", "Перепл. 209",
               "Долг 205", "Перепл. 205", "Итого начислено"]
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E9D5FF")
    for i, r in enumerate(rows, 2):
        u, room = r[0], r[1]
        ws.cell(row=i, column=1, value=u.id)
        ws.cell(row=i, column=2, value=u.username)
        ws.cell(row=i, column=3, value=(room.dormitory_name if room else ""))
        ws.cell(row=i, column=4, value=(room.room_number if room else ""))
        ws.cell(row=i, column=5, value=float(r[2] or 0))
        ws.cell(row=i, column=6, value=float(r[3] or 0))
        ws.cell(row=i, column=7, value=float(r[4] or 0))
        ws.cell(row=i, column=8, value=float(r[5] or 0))
        ws.cell(row=i, column=9, value=float(r[6] or 0))
    for col, w in [("A", 6), ("B", 30), ("C", 22), ("D", 10),
                   ("E", 12), ("F", 12), ("G", 12), ("H", 12), ("I", 14)]:
        ws.column_dimensions[col].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"debts_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# =========================================================================
# DEBT IMPORT HISTORY
# =========================================================================

@router.get("/debts/import-history", summary="История импортов 1С")
async def debts_import_history(
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_finance(current_user)
    logs = (await db.execute(
        select(DebtImportLog).order_by(desc(DebtImportLog.started_at)).limit(limit)
    )).scalars().all()
    return [
        {
            "id": log.id,
            "account_type": log.account_type,
            "file_name": log.file_name,
            "status": log.status,
            "started_by": log.started_by_username,
            "processed": log.processed,
            "updated": log.updated,
            "created": log.created,
            "not_found_count": log.not_found_count,
            "error": log.error,
            "started_at": log.started_at.isoformat() if log.started_at else None,
            "completed_at": log.completed_at.isoformat() if log.completed_at else None,
            "reverted_at": log.reverted_at.isoformat() if log.reverted_at else None,
        }
        for log in logs
    ]


@router.get("/debts/import-history/{log_id}/not-found", summary="Не найденные ФИО в импорте")
async def debts_not_found(
    log_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список ФИО из конкретного импорта, которые fuzzy не привязал.
    Админ может вручную сопоставить через reassign-endpoint."""
    _require_finance(current_user)
    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог импорта не найден")
    return {
        "log_id": log.id,
        "account_type": log.account_type,
        "not_found_users": log.not_found_users or [],
    }


@router.post("/debts/import-history/{log_id}/undo", summary="Откат импорта 1С")
async def debts_undo_import(
    log_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Восстанавливает debt_*/overpayment_* по snapshot и удаляет
    созданные импортом черновики. Только для админа/финансиста."""
    if current_user.role not in ("admin", "financier"):
        raise HTTPException(403, "Недостаточно прав для отката импорта")

    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог импорта не найден")
    if log.status != "completed":
        raise HTTPException(400, f"Нельзя откатить импорт в статусе «{log.status}»")
    if not log.snapshot_data:
        raise HTTPException(400, "Нет snapshot-данных — откат невозможен (старый импорт?)")

    before = log.snapshot_data.get("before", {})
    inserted_ids = log.snapshot_data.get("inserted_reading_ids", [])

    # 1. Восстанавливаем существующие readings из snapshot
    updates = []
    for reading_id_str, vals in before.items():
        updates.append({
            "id": int(reading_id_str),
            "debt_209": Decimal(vals.get("debt_209", "0")),
            "overpayment_209": Decimal(vals.get("overpayment_209", "0")),
            "debt_205": Decimal(vals.get("debt_205", "0")),
            "overpayment_205": Decimal(vals.get("overpayment_205", "0")),
        })

    # SQLAlchemy async не имеет bulk_update_mappings — делаем обычный update per row.
    # Для 1000+ записей не критично (один индексированный UPDATE).
    from sqlalchemy import update as _update
    for u in updates:
        await db.execute(
            _update(MeterReading)
            .where(MeterReading.id == u["id"])
            .values(
                debt_209=u["debt_209"],
                overpayment_209=u["overpayment_209"],
                debt_205=u["debt_205"],
                overpayment_205=u["overpayment_205"],
            )
        )

    # 2. Удаляем черновики, которые создал этот импорт
    # (берём только те, что всё ещё is_approved=False — согласованные не трогаем)
    if inserted_ids:
        from sqlalchemy import delete as _delete
        await db.execute(
            _delete(MeterReading).where(
                MeterReading.id.in_(inserted_ids),
                MeterReading.is_approved.is_(False),
            )
        )

    log.status = "reverted"
    log.reverted_at = datetime.utcnow()

    await db.commit()

    return {
        "status": "ok",
        "restored_readings": len(updates),
        "removed_drafts": len(inserted_ids),
    }


# =========================================================================
# REASSIGN «не найденный ФИО» → жилец
# =========================================================================

@router.post("/debts/import-history/{log_id}/reassign", summary="Привязать не-найденное ФИО к жильцу")
async def debts_reassign_not_found(
    log_id: int,
    fio: str = Form(..., description="Оригинальное ФИО из Excel"),
    user_id: int = Form(..., description="ID жильца, к которому привязать"),
    debt: float = Form(0),
    overpayment: float = Form(0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ручная привязка ФИО из not_found к жильцу + добавление значений
    долга/переплаты в черновик.

    Удаляет переданный `fio` из списка not_found_users лога.
    """
    _require_finance(current_user)

    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог импорта не найден")
    if log.status != "completed":
        raise HTTPException(400, f"Статус лога «{log.status}» — reassign только для completed")

    user = await db.get(User, user_id)
    if not user or not user.room_id:
        raise HTTPException(400, "Жилец не найден или не привязан к комнате")

    # Ищем / создаём черновик за период импорта
    reading = None
    if log.period_id:
        reading = (await db.execute(
            select(MeterReading).where(
                MeterReading.period_id == log.period_id,
                MeterReading.room_id == user.room_id,
            ).limit(1)
        )).scalars().first()

    debt_dec = Decimal(str(debt or 0))
    over_dec = Decimal(str(overpayment or 0))

    if reading:
        if log.account_type == "209":
            reading.debt_209 = (reading.debt_209 or Decimal("0")) + debt_dec
            reading.overpayment_209 = (reading.overpayment_209 or Decimal("0")) + over_dec
        else:
            reading.debt_205 = (reading.debt_205 or Decimal("0")) + debt_dec
            reading.overpayment_205 = (reading.overpayment_205 or Decimal("0")) + over_dec
    elif log.period_id:
        reading = MeterReading(
            user_id=user.id,
            room_id=user.room_id,
            period_id=log.period_id,
            is_approved=False,
            debt_209=debt_dec if log.account_type == "209" else Decimal("0"),
            overpayment_209=over_dec if log.account_type == "209" else Decimal("0"),
            debt_205=debt_dec if log.account_type == "205" else Decimal("0"),
            overpayment_205=over_dec if log.account_type == "205" else Decimal("0"),
        )
        db.add(reading)

    # Удаляем FIO из not_found_users
    nfu = list(log.not_found_users or [])
    nfu_new = [x for x in nfu if x.strip().lower() != fio.strip().lower()]
    if len(nfu_new) != len(nfu):
        log.not_found_users = nfu_new
        log.not_found_count = len(nfu_new)

    await db.commit()
    return {"status": "ok", "reading_id": reading.id if reading else None}


# =========================================================================
# RECONCILIATION — сверка показаний с долгами (для Центра анализа)
# =========================================================================

@router.get("/debts/reconcile", summary="Сверка: readings vs debts в активном периоде")
async def debts_reconcile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает 3 списка для вкладки «Сверка 1С» в Центре анализа:
      * readings_without_debts — есть reading, но в 1С долгов нет (ок, оплачено?)
      * debts_without_readings — в readings стоит долг, но reading не утверждён
      * last_import_not_found — ФИО из последнего импорта, не привязанные
    """
    _require_finance(current_user)

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    if not active_period:
        return {
            "period": None,
            "readings_without_debts": [],
            "debts_without_readings": [],
            "last_import_not_found": [],
        }

    # 1) readings_without_debts — approved=True и debt_*/overpayment_* все 0 и total_cost > 0
    #    (жильцу начислено что-то, но 1С не вернула долг = вероятно уже оплатили)
    q1 = (
        select(User.username, Room.dormitory_name, Room.room_number,
               MeterReading.total_cost, MeterReading.id)
        .select_from(MeterReading)
        .join(User, User.id == MeterReading.user_id)
        .outerjoin(Room, Room.id == MeterReading.room_id)
        .where(
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(True),
            MeterReading.total_cost > 0,
            MeterReading.debt_209 == 0,
            MeterReading.debt_205 == 0,
        )
        .order_by(desc(MeterReading.total_cost))
        .limit(100)
    )
    r_no_debts = [
        {
            "username": row[0], "dormitory": row[1], "room_number": row[2],
            "total_cost": float(row[3] or 0), "reading_id": row[4],
        }
        for row in (await db.execute(q1)).all()
    ]

    # 2) debts_without_readings — долг есть (debt_209+205 > 0), но reading не approved
    q2 = (
        select(User.username, Room.dormitory_name, Room.room_number,
               MeterReading.debt_209, MeterReading.debt_205, MeterReading.id)
        .select_from(MeterReading)
        .join(User, User.id == MeterReading.user_id)
        .outerjoin(Room, Room.id == MeterReading.room_id)
        .where(
            MeterReading.period_id == active_period.id,
            MeterReading.is_approved.is_(False),
            (MeterReading.debt_209 > 0) | (MeterReading.debt_205 > 0),
        )
        .order_by(desc(MeterReading.debt_209 + MeterReading.debt_205))
        .limit(100)
    )
    d_no_readings = [
        {
            "username": row[0], "dormitory": row[1], "room_number": row[2],
            "debt_209": float(row[3] or 0), "debt_205": float(row[4] or 0),
            "reading_id": row[5],
        }
        for row in (await db.execute(q2)).all()
    ]

    # 3) Последний успешный импорт — not_found FIO
    last_log = (await db.execute(
        select(DebtImportLog)
        .where(DebtImportLog.status == "completed")
        .order_by(desc(DebtImportLog.started_at)).limit(1)
    )).scalars().first()
    nf = (last_log.not_found_users or []) if last_log else []

    return {
        "period": {"id": active_period.id, "name": active_period.name},
        "readings_without_debts": r_no_debts,
        "debts_without_readings": d_no_readings,
        "last_import_not_found": nf[:200],
        "last_import_id": last_log.id if last_log else None,
    }