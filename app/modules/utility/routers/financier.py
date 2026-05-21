import os
import uuid
import asyncio
import logging
from decimal import Decimal
from app.core.time_utils import utcnow
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
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

def _nfu_fio(item) -> str:
    """Извлекает ФИО из элемента not_found_users.

    not_found_users поменял формат: legacy = list[str], новый = list[dict].
    Помогает не падать с AttributeError при работе со старыми импортами
    и при удалении/сравнении элементов.
    """
    if isinstance(item, dict):
        return str(item.get("fio", "")).strip()
    return str(item).strip()


async def _ensure_debt_alias(
    db: AsyncSession,
    *,
    alias_fio: str,
    user_id: int,
    created_by_id: int,
    note: Optional[str] = None,
) -> bool:
    """Создаёт GSheetsAlias для запоминания привязки ФИО → user.

    Общая таблица для всех типов импорта (gsheets, debt 205, debt 209) —
    привязка сделана один раз, работает везде. Если alias по этому
    normalized ФИО уже есть (даже на другого юзера) — НЕ перезаписываем,
    оставляем старый (защита от случайной перебивки чужой привязки).

    Возвращает True если действительно создал новую запись.
    """
    from app.modules.utility.models import GSheetsAlias
    from app.modules.utility.services.gsheets_sync import normalize_fio

    normalized = normalize_fio(alias_fio)
    if not normalized:
        return False
    existing = (await db.execute(
        select(GSheetsAlias).where(GSheetsAlias.alias_fio_normalized == normalized)
    )).scalars().first()
    if existing:
        return False

    db.add(GSheetsAlias(
        alias_fio=alias_fio.strip(),
        alias_fio_normalized=normalized,
        user_id=user_id,
        kind="debt_manual",
        note=note,
        created_by_id=created_by_id,
    ))
    return True


TEMP_DIR = "/app/static/temp_imports"  # legacy, для совместимости со старым кодом
# Постоянное хранение оригиналов ОСВ из 1С.
# ПУТЬ: используем существующий shared_data volume (/app/static/generated_files/).
# Раньше пробовали /app/data/debt_archives через отдельный volume,
# но это требовало `docker compose down && up -d` для создания volume —
# до рестарта web писал в эфемерный слой, а worker_heavy искал в своём
# эфемерном слое, выдавая «No such file or directory». shared_data уже
# смонтирован на web + worker_heavy + nginx, файлы шарятся сразу.
#
# Прямой доступ через nginx (/static/generated_files/debt_archives/...)
# заблокирован в nginx/conf.d/default.conf — скачивание только через
# /api/financier/debts/import-history/{id}/download с auth.
DEBT_ARCHIVE_DIR = "/app/static/generated_files/debt_archives"
MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(DEBT_ARCHIVE_DIR, exist_ok=True)


async def _save_uploaded_debt_file(
    file: UploadFile, account_type: str, batch_id: str
) -> tuple[str, str]:
    """Валидирует, проверяет размер/magic-bytes и сохраняет xlsx в archive.

    Возвращает (file_path, original_name). Кидает HTTPException при ошибке.
    Файлы парного импорта получают batch_id в имени для группировки на
    диске — после успешного импорта debt_import.py зальёт archive_path
    в DebtImportLog.
    """
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
            detail=f"Файл слишком большой. Максимум {MAX_FILE_SIZE / 1024 / 1024} MB",
        )

    ext = file.filename.rsplit(".", 1)[-1].lower()
    unique_name = f"{batch_id}_{account_type}.{ext}"
    file_path = os.path.join(DEBT_ARCHIVE_DIR, unique_name)

    try:
        content = await file.read()

        def save_file():
            with open(file_path, "wb") as buffer:
                buffer.write(content)

        await asyncio.to_thread(save_file)
    except Exception:
        logger.exception("Failed to save uploaded file")
        raise HTTPException(status_code=500, detail="Ошибка сохранения файла")

    return file_path, file.filename


@router.post("/import-debts", summary="Фоновый импорт долгов из 1С")
async def upload_debts_1c(
        account_type: str = Form(..., pattern="^(209|205)$", description="Тип счета: 209 или 205"),
        file: UploadFile = File(...),
        current_user: User = Depends(get_current_user)
):
    """Загрузка ОДНОГО файла. Для парной загрузки 205+209 используйте
    /import-debts-pair — он создаёт один batch_id для обоих импортов
    и UI показывает их как единую операцию."""
    if current_user.role not in ("financier", "accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    batch_id = str(uuid.uuid4())
    file_path, original_name = await _save_uploaded_debt_file(file, account_type, batch_id)

    task = import_debts_task.delay(
        file_path, account_type,
        started_by_id=current_user.id,
        started_by_username=current_user.username,
        batch_id=batch_id,
        original_file_name=original_name,
    )
    logger.info(f"[IMPORT] Started task={task.id} for account={account_type} batch={batch_id}")

    return {
        "task_id": task.id,
        "status": "processing",
        "account_type": account_type,
        "batch_id": batch_id,
    }


@router.post("/import-debts-pair", summary="Парная загрузка 205 + 209 одной операцией")
async def upload_debts_pair_1c(
        file_209: UploadFile = File(None, description="Файл ОСВ по счёту 209"),
        file_205: UploadFile = File(None, description="Файл ОСВ по счёту 205"),
        current_user: User = Depends(get_current_user),
):
    """Загружает ОБА файла одной операцией. Оба DebtImportLog получают
    один batch_id — в истории импортов показываются как одна группа.

    Минимум один файл должен быть передан. Если только один — работает
    как обычный /import-debts (но всё равно создаёт batch_id для
    унификации UI).
    """
    if current_user.role not in ("financier", "accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    if not file_209 and not file_205:
        raise HTTPException(status_code=400, detail="Передайте хотя бы один файл (209 и/или 205)")

    batch_id = str(uuid.uuid4())
    tasks_out = []

    # СЕРИАЛИЗАЦИЯ через celery chain (may 2026):
    # Раньше тут было `import_debts_task.delay(...)` для каждого файла в цикле —
    # ДВА task'а запускались параллельно. При concurrency > 1 worker'ы
    # одновременно SELECT'или readings, видели «у жильца нет reading в active
    # period», оба шли в inserts_dict через db.add_all. Затем при commit:
    #   - либо unique-violation на (user_id, room_id, period_id) → второй task
    #     падал с rollback, debt_205 терялся ✗
    #   - либо без unique → два дубля reading; UI выбирал первый ✗
    #
    # Симптом: Лучка А.П. — debt_209=21889 виден (первый task), debt_205=0
    # (второй task упал/конфликтнул, но статус completed).
    #
    # Теперь — chain через .si() (immutable signature, не передаёт return
    # первого как arg второго). 205 task стартует ТОЛЬКО когда 209 task
    # успешно завершён и commit'нул. Никаких race condition'ов.
    from celery import chain
    signatures = []
    task_meta = []  # сохраняем file_path/account для построения сигнатур

    for f, account in [(file_209, "209"), (file_205, "205")]:
        if f is None:
            continue
        file_path, original_name = await _save_uploaded_debt_file(f, account, batch_id)
        sig = import_debts_task.si(
            file_path, account,
            started_by_id=current_user.id,
            started_by_username=current_user.username,
            batch_id=batch_id,
            original_file_name=original_name,
        )
        signatures.append(sig)
        task_meta.append({"account": account, "file_name": original_name})

    if not signatures:
        return {"status": "noop", "batch_id": batch_id, "tasks": []}

    # Запускаем последовательно. apply_async() возвращает AsyncResult последнего
    # task'а в цепочке — фронт поллит его, и когда он SUCCESS → значит вся
    # цепочка отработала.
    chain_result = chain(*signatures).apply_async()

    # Собираем id всех task'ов в цепочке (для UI). Первый task — chain_result.parent...
    # цикл наверх. В celery chain цепочка прицеплена через .parent.
    task_ids = []
    cur = chain_result
    while cur is not None:
        task_ids.append(cur.id)
        cur = cur.parent
    task_ids.reverse()  # порядок исполнения

    for meta, tid in zip(task_meta, task_ids):
        tasks_out.append({
            "account_type": meta["account"],
            "task_id": tid,
            "file_name": meta["file_name"],
        })
        logger.info(
            f"[IMPORT-PAIR] Queued task={tid} for account={meta['account']} "
            f"batch={batch_id} (sequential chain)"
        )

    return {
        "status": "processing",
        "batch_id": batch_id,
        "tasks": tasks_out,
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
    fname = f"debts_{utcnow().strftime('%Y%m%d_%H%M')}.xlsx"
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
            # Новые поля: парный batch + наличие оригинала для скачивания
            "batch_id": log.batch_id,
            "has_archive": bool(log.archive_path),
        }
        for log in logs
    ]


@router.get("/debts/import-history/{log_id}/diff", summary="Diff с предыдущим импортом того же счёта")
async def debts_import_diff(
    log_id: int,
    against_id: Optional[int] = Query(None, description="ID импорта для сравнения. None — предыдущий того же типа."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Сравнивает applied_state двух импортов одного account_type.

    Категории жильцов:
      - new_debtors:  не было в прошлом импорте, появился долг > 0
      - debt_grew:    был и есть, debt вырос
      - debt_dropped: был и есть, debt упал (но > 0)
      - debt_closed:  был долг > 0, стал 0 (или жилец исчез из файла)
      - new_overpay:  появилась переплата которой не было

    На жильцов с одинаковой суммой не возвращаем — это шум, отрисуется
    только то что изменилось.
    """
    _require_finance(current_user)

    current = await db.get(DebtImportLog, log_id)
    if not current:
        raise HTTPException(404, "Лог не найден")
    if not current.applied_state:
        raise HTTPException(
            400,
            "У этого импорта нет applied_state (импорт до миграции debts_003). "
            "Перезагрузите файлы — diff заработает.",
        )

    # Находим предыдущий импорт того же account_type, либо берём указанный.
    if against_id is not None:
        previous = await db.get(DebtImportLog, against_id)
        if not previous:
            raise HTTPException(404, "Лог для сравнения не найден")
        if previous.account_type != current.account_type:
            raise HTTPException(
                400,
                f"Нельзя сравнивать {previous.account_type!r} с {current.account_type!r}",
            )
    else:
        previous = (await db.execute(
            select(DebtImportLog)
            .where(
                DebtImportLog.account_type == current.account_type,
                DebtImportLog.id < current.id,
                DebtImportLog.status == "completed",
                DebtImportLog.applied_state.is_not(None),
            )
            .order_by(desc(DebtImportLog.id))
            .limit(1)
        )).scalars().first()

    if not previous:
        return {
            "current_id": log_id,
            "previous_id": None,
            "account_type": current.account_type,
            "fatal": "Это первый импорт этого счёта — сравнивать не с чем.",
        }

    cur_state = current.applied_state or {}
    prev_state = previous.applied_state or {}
    account = current.account_type
    debt_key = f"debt_{account}"
    over_key = f"overpayment_{account}"

    def _dec(d, key):
        try:
            return Decimal(str(d.get(key, "0") or "0"))
        except Exception:
            return Decimal("0")

    new_debtors = []
    debt_grew = []
    debt_dropped = []
    debt_closed = []
    new_overpay = []

    all_room_ids = set(cur_state.keys()) | set(prev_state.keys())
    for room_id in all_room_ids:
        cur = cur_state.get(room_id, {})
        prev = prev_state.get(room_id, {})
        cur_debt = _dec(cur, debt_key)
        prev_debt = _dec(prev, debt_key)
        cur_over = _dec(cur, over_key)
        prev_over = _dec(prev, over_key)

        # Метаданные берём из cur если есть, иначе из prev (если жилец исчез)
        meta_username = cur.get("username") or prev.get("username") or "—"
        meta_room = cur.get("room_label") or prev.get("room_label") or "—"

        if cur_debt > prev_debt:
            entry = {
                "room_id": int(room_id),
                "username": meta_username,
                "room_label": meta_room,
                "prev_debt": float(prev_debt),
                "current_debt": float(cur_debt),
                "delta": float(cur_debt - prev_debt),
            }
            if prev_debt == 0 and cur_debt > 0:
                new_debtors.append(entry)
            else:
                debt_grew.append(entry)
        elif cur_debt < prev_debt:
            entry = {
                "room_id": int(room_id),
                "username": meta_username,
                "room_label": meta_room,
                "prev_debt": float(prev_debt),
                "current_debt": float(cur_debt),
                "delta": float(cur_debt - prev_debt),  # отрицательная
            }
            if cur_debt == 0 and prev_debt > 0:
                debt_closed.append(entry)
            else:
                debt_dropped.append(entry)

        # Появилась переплата которой не было — сигнал что админ должен возвратить
        if cur_over > 0 and prev_over == 0:
            new_overpay.append({
                "room_id": int(room_id),
                "username": meta_username,
                "room_label": meta_room,
                "overpayment": float(cur_over),
            })

    # Сортируем: новые должники и рост — по сумме убыванию
    new_debtors.sort(key=lambda x: -x["current_debt"])
    debt_grew.sort(key=lambda x: -x["delta"])
    debt_dropped.sort(key=lambda x: x["delta"])  # самый большой спад первым
    debt_closed.sort(key=lambda x: -x["prev_debt"])
    new_overpay.sort(key=lambda x: -x["overpayment"])

    # Топ-снимок сумм для KPI
    sum_grew = sum(e["delta"] for e in debt_grew + new_debtors)
    sum_closed = sum(e["prev_debt"] for e in debt_closed)
    sum_dropped = sum(-e["delta"] for e in debt_dropped)

    return {
        "current_id": log_id,
        "previous_id": previous.id,
        "account_type": account,
        "current_started_at": current.started_at.isoformat() if current.started_at else None,
        "previous_started_at": previous.started_at.isoformat() if previous.started_at else None,
        "summary": {
            "new_debtors_count": len(new_debtors),
            "debt_grew_count": len(debt_grew),
            "debt_dropped_count": len(debt_dropped),
            "debt_closed_count": len(debt_closed),
            "new_overpay_count": len(new_overpay),
            "sum_new_and_grew": float(sum_grew),
            "sum_closed": float(sum_closed),
            "sum_dropped": float(sum_dropped),
        },
        # Лимиты на размер response — UI всё равно покажет первые 100
        "new_debtors": new_debtors[:100],
        "debt_grew": debt_grew[:100],
        "debt_dropped": debt_dropped[:100],
        "debt_closed": debt_closed[:100],
        "new_overpay": new_overpay[:50],
    }


@router.get("/debts/user-debt-history/{user_id}", summary="История долгов жильца через все импорты")
async def debts_user_history(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает точки графика для одного жильца: на каждый
    completed-импорт — debt+overpayment по этому юзеру (по его room_id).

    Сортировка по started_at. Если у жильца менялась комната — она
    учитывается: ищем applied_state по room_id, который был у жильца
    на момент импорта. Для простоты MVP берём текущий room_id юзера —
    это покрывает 99% случаев (миграции редки).

    UI рисует две линии: 209 (коммунальный) и 205 (найм), плюс tabular
    разрез по каждому импорту.
    """
    _require_finance(current_user)

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Жилец не найден")
    if not user.room_id:
        return {
            "user_id": user_id,
            "username": user.username,
            "room_id": None,
            "points": [],
            "fatal": "У жильца нет комнаты — долги привязываются к комнате, не к юзеру.",
        }

    room_id_key = str(user.room_id)

    # Все completed-импорты с applied_state — отсортированы по дате
    logs = (await db.execute(
        select(DebtImportLog)
        .where(
            DebtImportLog.status == "completed",
            DebtImportLog.applied_state.is_not(None),
        )
        .order_by(DebtImportLog.started_at.asc(), DebtImportLog.id.asc())
    )).scalars().all()

    points = []
    last_room_label = None
    for log in logs:
        st = log.applied_state or {}
        entry = st.get(room_id_key)
        if not entry:
            # В этот импорт этой комнаты не было — пропускаем точку, чтобы
            # не подмешивать «0», которое на самом деле «нет данных».
            continue
        debt_key = f"debt_{log.account_type}"
        over_key = f"overpayment_{log.account_type}"
        try:
            debt = float(Decimal(str(entry.get(debt_key, "0") or "0")))
            over = float(Decimal(str(entry.get(over_key, "0") or "0")))
        except Exception:
            debt = 0.0
            over = 0.0
        # room_label берём из applied_state (denormalized), чтобы не делать
        # отдельный JOIN. Последнее значение — самое свежее.
        if entry.get("room_label"):
            last_room_label = entry["room_label"]
        points.append({
            "log_id": log.id,
            "started_at": log.started_at.isoformat() if log.started_at else None,
            "account_type": log.account_type,
            "debt": debt,
            "overpayment": over,
            "file_name": log.file_name,
        })

    return {
        "user_id": user_id,
        "username": user.username,
        "room_id": user.room_id,
        "room_label": last_room_label,
        "points": points,
    }


@router.get("/debts/import-history/{log_id}/download", summary="Скачать оригинальный xlsx")
async def debts_import_download(
    log_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Отдаёт оригинальный файл ОСВ из 1С, привязанный к этому импорту.

    archive_path хранится на диске вне /static (защита от прямого
    скачивания через nginx без auth). FileResponse кидает 404 если файл
    физически удалён (например, после retention-чистки).
    """
    _require_finance(current_user)
    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог импорта не найден")
    if not log.archive_path:
        raise HTTPException(
            404,
            "Архив этого импорта не сохранён (старый импорт до миграции debts_002)",
        )
    if not os.path.exists(log.archive_path):
        raise HTTPException(
            404,
            "Файл физически удалён (retention-policy / ручная очистка).",
        )

    # Имя для пользователя: оригинальное file_name если есть, иначе генерим
    # понятное «debts_209_2026-05-12.xlsx».
    download_name = log.file_name or (
        f"debts_{log.account_type}_{log.started_at.strftime('%Y-%m-%d') if log.started_at else 'unknown'}.xlsx"
    )

    from fastapi.responses import FileResponse
    return FileResponse(
        path=log.archive_path,
        filename=download_name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/debts/import-history/{log_id}/not-found", summary="Не найденные ФИО в импорте")
async def debts_not_found(
    log_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Список ФИО из конкретного импорта, которые fuzzy не привязал.
    Админ может вручную сопоставить через reassign-endpoint.

    Формат not_found_users поменялся в импорте мая 2026:
      - старые импорты: list[str] — только ФИО, без сумм
      - новые: list[dict] {fio, debt, overpayment} — фронт префиллит инпуты
    Возвращаем УНИФИЦИРОВАННЫЙ формат list[dict] чтобы UI был один.
    """
    _require_finance(current_user)
    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог импорта не найден")

    raw_list = log.not_found_users or []
    normalized = []
    for item in raw_list:
        if isinstance(item, dict):
            normalized.append({
                "fio": item.get("fio", ""),
                "debt": item.get("debt", "0"),
                "overpayment": item.get("overpayment", "0"),
            })
        else:
            # Legacy: только ФИО, без сумм. Админу придётся вводить руками.
            normalized.append({
                "fio": str(item),
                "debt": "0",
                "overpayment": "0",
            })

    return {
        "log_id": log.id,
        "account_type": log.account_type,
        "not_found_users": normalized,
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
    log.reverted_at = utcnow()

    await db.commit()

    return {
        "status": "ok",
        "restored_readings": len(updates),
        "removed_drafts": len(inserted_ids),
    }


@router.delete("/debts/import-history/{log_id}", summary="Удалить запись истории импорта 1С")
async def debts_delete_import_history(
    log_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Удаляет запись DebtImportLog **без** отката данных.

    Use case: после массового rebuild / reload-period долги в БД уже
    сброшены и заново импортированы. Старые записи истории «висят» с
    цифрами +N₽/+M₽, но реально debt'ы уже не соответствуют. Кнопка
    «Откатить» в таком случае бесполезна (snapshot устаревший). Эта
    кнопка просто удаляет запись из списка истории.

    Защита: если статус completed (актуальный импорт) — требуется
    подтверждение через ?confirm=YES.
    """
    if current_user.role not in ("admin", "financier"):
        raise HTTPException(403, "Недостаточно прав")

    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог не найден")

    from fastapi import Query as _Q  # noqa: F401 (для документации)
    # confirm передаётся через query string
    from sqlalchemy import delete as _delete
    await db.execute(_delete(DebtImportLog).where(DebtImportLog.id == log_id))
    await db.commit()
    return {"status": "ok", "deleted_id": log_id}


@router.post("/debts/import-history/cleanup", summary="Массовая чистка истории импортов")
async def debts_cleanup_import_history(
    keep_last: int = 5,
    only_reverted: bool = False,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Массовое удаление устаревших записей DebtImportLog.

    Параметры:
      keep_last     — сколько последних completed-импортов на каждый
                      account_type сохранять (по умолчанию 5);
      only_reverted — если True, удаляются ТОЛЬКО reverted-записи
                      (откаченные), completed не трогаются.

    Use case пользователя: после наших rebuild/reload-period в истории
    висят откаченные импорты + устаревшие completed (debt'ы в БД уже
    обновлены последним импортом). UI показывает «№23, №24 Откачен»
    мусором — этот endpoint их выпиливает.
    """
    if current_user.role not in ("admin", "financier"):
        raise HTTPException(403, "Недостаточно прав")

    from sqlalchemy import delete as _delete

    if only_reverted:
        # Удаляем все reverted/failed.
        res = await db.execute(
            _delete(DebtImportLog).where(
                DebtImportLog.status.in_(["reverted", "failed"])
            )
        )
        deleted = res.rowcount or 0
    else:
        # Удаляем reverted/failed ВСЕ + у каждого account_type оставляем
        # последние keep_last completed.
        # 1. Снести reverted/failed.
        await db.execute(
            _delete(DebtImportLog).where(
                DebtImportLog.status.in_(["reverted", "failed"])
            )
        )
        # 2. По account_type: оставить keep_last свежих completed, остальное удалить.
        for acct in ("209", "205"):
            completed = (await db.execute(
                select(DebtImportLog.id)
                .where(
                    DebtImportLog.account_type == acct,
                    DebtImportLog.status == "completed",
                )
                .order_by(desc(DebtImportLog.id))
            )).scalars().all()
            to_delete = completed[keep_last:]
            if to_delete:
                await db.execute(
                    _delete(DebtImportLog).where(DebtImportLog.id.in_(to_delete))
                )
        # Re-count.
        remaining = (await db.execute(
            select(func.count(DebtImportLog.id))
        )).scalar_one()
        deleted = -1  # неизвестно — не критично для UI
        deleted = max(0, deleted)
        await db.commit()
        return {"status": "ok", "remaining": int(remaining)}

    await db.commit()
    return {"status": "ok", "deleted": deleted}


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

    # Удаляем FIO из not_found_users. После фикса формата (list[dict])
    # сравниваем через helper _nfu_fio чтобы не упасть на .strip() от dict.
    nfu = list(log.not_found_users or [])
    fio_norm = fio.strip().lower()
    nfu_new = [x for x in nfu if _nfu_fio(x).lower() != fio_norm]
    if len(nfu_new) != len(nfu):
        log.not_found_users = nfu_new
        log.not_found_count = len(nfu_new)

    # Сохраняем alias чтобы при СЛЕДУЮЩЕМ импорте (205 или 209 или gsheets)
    # эта же ФИО автоматически матчилась на user — без повторного reassign.
    alias_created = await _ensure_debt_alias(
        db, alias_fio=fio, user_id=user_id,
        created_by_id=current_user.id,
        note=f"debt reassign log#{log_id}",
    )

    await db.commit()
    return {
        "status": "ok",
        "reading_id": reading.id if reading else None,
        "alias_created": alias_created,
    }


# =========================================================================
# FIND CANDIDATES — поиск похожих жильцов для not-found ФИО
# =========================================================================
@router.get("/debts/find-candidates", summary="Похожие жильцы по ФИО (fuzzy + фамилия)")
async def debts_find_candidates(
    fio: Optional[str] = Query(None, max_length=200, description="ФИО из Excel (для auto-suggest)"),
    q: Optional[str] = Query(None, max_length=100, description="Ручной поиск по любой подстроке"),
    limit: int = Query(15, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает кандидатов для модалки «Не найдены в 1С».

    Два режима:
      1) `q`  — ручной поиск, ILIKE по подстроке (case-insensitive). Когда
                админ вбивает часть фамилии или имени в input поиска.
      2) `fio` — auto-suggest для импорта. Объединяет:
                 - ТОЧНОЕ совпадение фамилии (первого токена) → score=100
                 - fuzzy token_sort_ratio для остальных (threshold 40%)
                 Это решает кейс когда в Excel «Ярощук Александр Павлович»,
                 а в БД «Ярощук А.П.» — fuzzy один даёт score~50%, и жилец
                 теряется в кандидатах с такими же score; surname-match
                 поднимает его в топ.

    Хотя бы один из q/fio должен быть задан.
    """
    _require_finance(current_user)

    if not q and not fio:
        raise HTTPException(400, "Передайте q (ручной поиск) или fio (по импорту)")

    from sqlalchemy.orm import selectinload as _selectinload

    base_query = (
        select(User).options(_selectinload(User.room))
        .where(User.is_deleted.is_(False), User.role == "user")
    )

    # ============= РЕЖИМ 1: ручной поиск по q =============
    if q:
        q_norm = q.strip().lower()
        if len(q_norm) < 2:
            return {"fio": None, "q": q, "candidates": []}
        # ILIKE по нормализованному username. Допускаем многословный q
        # — каждый токен должен встречаться (AND).
        tokens = [t for t in q_norm.split() if t]
        filtered = base_query
        for tok in tokens:
            filtered = filtered.where(func.lower(User.username).like(f"%{tok}%"))
        users_raw = (await db.execute(filtered.limit(limit))).scalars().all()

        candidates = []
        for u in users_raw:
            room_label = (
                f"{u.room.dormitory_name} / {u.room.room_number}"
                if u.room else "без комнаты"
            )
            candidates.append({
                "id": u.id,
                "username": u.username,
                "room_label": room_label,
                "residents_count": int(u.residents_count or 1),
                "score": 100,  # точное substring-совпадение
                "reason": None,
            })
        # Сортируем по username (стабильный порядок)
        candidates.sort(key=lambda c: c["username"].lower())
        return {"fio": None, "q": q, "candidates": candidates}

    # ============= РЕЖИМ 2: auto-suggest по fio =============
    from rapidfuzz import fuzz

    target_norm = " ".join(fio.lower().split())
    if not target_norm:
        return {"fio": fio, "candidates": []}
    target_tokens = target_norm.split()
    surname = target_tokens[0] if target_tokens else ""

    users_raw = (await db.execute(base_query)).scalars().all()

    # Проходим всех жильцов. Для каждого считаем score по двум критериям:
    #   - точное совпадение фамилии (первый токен username) → 100
    #   - token_sort_ratio
    # Из двух берём максимум.
    matches: list[tuple[User, int, Optional[str]]] = []
    for u in users_raw:
        if not u.username:
            continue
        name_norm = " ".join(u.username.lower().split())
        name_tokens = name_norm.split()

        # Точное совпадение фамилии — приоритет
        surname_exact = (
            surname and name_tokens and surname == name_tokens[0]
        )
        # Или фамилия как substring в username (защита от опечаток типа
        # «Ярощук-Иванов» когда в БД двойная фамилия)
        surname_substring = (
            surname and len(surname) >= 4 and surname in name_norm
        )

        fuzzy_score = fuzz.token_sort_ratio(target_norm, name_norm)

        if surname_exact:
            score = max(100, fuzzy_score)
            reason = "Совпадает фамилия" if fuzzy_score < 80 else None
        elif surname_substring:
            score = max(85, fuzzy_score)
            reason = "Фамилия найдена внутри ФИО"
        elif fuzzy_score >= 40:
            score = fuzzy_score
            # «Общее отчество» — простая эвристика для случая брат/сестра
            reason = None
            if (fuzzy_score < 80 and len(target_tokens) >= 3
                    and len(name_tokens) >= 3
                    and target_tokens[-1] == name_tokens[-1]
                    and target_tokens[0] != name_tokens[0]):
                reason = "Общее отчество (возможно, брат/сестра)"
        else:
            continue

        matches.append((u, int(score), reason))

    # Сортируем: сначала score DESC, потом username для стабильности
    matches.sort(key=lambda m: (-m[1], m[0].username.lower()))
    matches = matches[:limit]

    candidates = []
    for u, score, reason in matches:
        room_label = (
            f"{u.room.dormitory_name} / {u.room.room_number}"
            if u.room else "без комнаты"
        )
        candidates.append({
            "id": u.id,
            "username": u.username,
            "room_label": room_label,
            "residents_count": int(u.residents_count or 1),
            "score": score,
            "reason": reason,
        })

    return {"fio": fio, "candidates": candidates}


# =========================================================================
# CREATE-AND-MATCH — создать нового жильца + привязать долг
# =========================================================================
class DebtCreateAndMatchRequest(BaseModel):
    """Создание нового жильца с одновременной привязкой долга/переплаты.

    Аналогично gsheets create-and-match (admin_gsheets.py), но здесь сразу
    добавляем сумму к debt_*/overpayment_* в MeterReading периода импорта.
    """
    fio: str           # ФИО из Excel — для удаления из not_found_users
    username: str      # логин для входа
    password: str
    dormitory_name: str
    room_number: str
    debt: float = 0.0
    overpayment: float = 0.0
    residents_count: int = 1
    resident_type: str = "family"
    workplace: Optional[str] = None


@router.post("/debts/import-history/{log_id}/create-and-match", summary="Создать жильца + привязать долг")
async def debts_create_and_match(
    log_id: int,
    data: DebtCreateAndMatchRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Создаёт нового User + комнатную привязку + добавляет долг к
    черновику reading периода импорта. Удаляет ФИО из not_found_users.

    Используется когда жильца РЕАЛЬНО нет в системе (новый человек,
    которого ещё не завели в «Жильцы»). До этого фикса админ должен был:
      1) зайти в «Жильцы»
      2) создать пользователя
      3) вернуться в долги и сделать reassign
    Сейчас всё одной операцией.
    """
    _require_finance(current_user)

    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог не найден")
    if log.status != "completed":
        raise HTTPException(400, f"Статус лога «{log.status}» — операция только для completed")

    # 1. Уникальность логина (case-insensitive)
    existing_user = (await db.execute(
        select(User).where(func.lower(User.username) == data.username.strip().lower())
    )).scalars().first()
    if existing_user:
        raise HTTPException(400, f"Логин «{data.username.strip()}» уже занят")

    # 2. Комната должна существовать в Жилфонде
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
            "не найдена в Жилфонде. Создайте её сначала.",
        )

    # 3. Создание User
    rt = data.resident_type if data.resident_type in ("family", "single") else "family"
    bm = "per_capita" if rt == "single" else "by_meter"

    from app.core.auth import get_password_hash
    db_user = User(
        username=data.username.strip(),
        hashed_password=get_password_hash(data.password),
        role="user",
        workplace=(data.workplace or "").strip() or None,
        residents_count=max(1, int(data.residents_count)),
        room_id=None,  # выставит move_user_to_room
        resident_type=rt,
        billing_mode=bm,
        is_deleted=False,
        is_initial_setup_done=False,
    )
    db.add(db_user)
    await db.flush()

    from app.modules.utility.services.room_assignment import move_user_to_room
    await move_user_to_room(
        db, user=db_user, new_room_id=room.id,
        note=f"created via debts not-found import #{log_id}",
    )

    # 4. Добавляем долг к черновику reading периода импорта
    debt_dec = Decimal(str(data.debt or 0))
    over_dec = Decimal(str(data.overpayment or 0))

    if log.period_id:
        reading = (await db.execute(
            select(MeterReading).where(
                MeterReading.period_id == log.period_id,
                MeterReading.room_id == room.id,
            ).limit(1)
        )).scalars().first()

        if reading:
            if log.account_type == "209":
                reading.debt_209 = (reading.debt_209 or Decimal("0")) + debt_dec
                reading.overpayment_209 = (reading.overpayment_209 or Decimal("0")) + over_dec
            else:
                reading.debt_205 = (reading.debt_205 or Decimal("0")) + debt_dec
                reading.overpayment_205 = (reading.overpayment_205 or Decimal("0")) + over_dec
        else:
            reading = MeterReading(
                user_id=db_user.id,
                room_id=room.id,
                period_id=log.period_id,
                is_approved=False,
                debt_209=debt_dec if log.account_type == "209" else Decimal("0"),
                overpayment_209=over_dec if log.account_type == "209" else Decimal("0"),
                debt_205=debt_dec if log.account_type == "205" else Decimal("0"),
                overpayment_205=over_dec if log.account_type == "205" else Decimal("0"),
            )
            db.add(reading)

    # 5. Удаляем FIO из not_found_users (см. _nfu_fio про list[dict] vs str)
    nfu = list(log.not_found_users or [])
    fio_norm = data.fio.strip().lower()
    nfu_new = [x for x in nfu if _nfu_fio(x).lower() != fio_norm]
    if len(nfu_new) != len(nfu):
        log.not_found_users = nfu_new
        log.not_found_count = len(nfu_new)

    # 6. Alias — то же что в reassign: запомнить эту привязку для будущего.
    await _ensure_debt_alias(
        db, alias_fio=data.fio, user_id=db_user.id,
        created_by_id=current_user.id,
        note=f"debt create-and-match log#{log_id}",
    )

    await db.commit()
    return {
        "status": "ok",
        "user_id": db_user.id,
        "username": data.username.strip(),
        "room_id": room.id,
    }


# =========================================================================
# RESET BALANCE — обнулить баланс конкретного жильца
# =========================================================================
@router.post("/users/{user_id}/reset-balance")
async def reset_user_balance(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Обнуляет debt_209/debt_205/overpayment_209/overpayment_205 у ВСЕХ
    reading-ов жильца (по room_id). Полезно когда после отката импорта
    у жильца остались «зависшие» сальдо от старых reading-ов в других
    периодах — undo конкретного импорта восстанавливает snapshot только
    для тех reading-ов, которые этот импорт трогал.

    Returns: количество reading-ов обнулённых + до-сальдо для аудита.
    """
    _require_finance(current_user)

    user = await db.get(User, user_id)
    if not user or not user.room_id:
        raise HTTPException(404, "Жилец не найден или без комнаты")

    # Снимаем before-snapshot для audit_log (на случай если нужно откатить)
    readings = (await db.execute(
        select(MeterReading).where(
            MeterReading.room_id == user.room_id,
            (MeterReading.debt_209 > 0)
            | (MeterReading.debt_205 > 0)
            | (MeterReading.overpayment_209 > 0)
            | (MeterReading.overpayment_205 > 0),
        )
    )).scalars().all()

    if not readings:
        return {
            "status": "noop",
            "user_id": user_id,
            "username": user.username,
            "reset_count": 0,
        }

    snapshot = []
    for r in readings:
        snapshot.append({
            "reading_id": r.id,
            "period_id": r.period_id,
            "debt_209": str(r.debt_209 or 0),
            "overpayment_209": str(r.overpayment_209 or 0),
            "debt_205": str(r.debt_205 or 0),
            "overpayment_205": str(r.overpayment_205 or 0),
        })
        r.debt_209 = Decimal("0.00")
        r.overpayment_209 = Decimal("0.00")
        r.debt_205 = Decimal("0.00")
        r.overpayment_205 = Decimal("0.00")
        # total_cost синхронизируется триггером (integrity_002)

    # Audit log
    from app.modules.utility.routers.admin_dashboard import write_audit_log
    await write_audit_log(
        db, current_user.id, current_user.username,
        action="reset_user_balance", entity_type="user", entity_id=user_id,
        details={"reset_count": len(readings), "snapshot": snapshot},
    )

    await db.commit()
    return {
        "status": "ok",
        "user_id": user_id,
        "username": user.username,
        "reset_count": len(readings),
    }


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
    # not_found_users может быть list[str] (legacy) или list[dict] (new).
    # Для reconcile-UI возвращаем плоский list[str] — там сейчас не нужны
    # суммы (только список ФИО).
    nf_raw = (last_log.not_found_users or []) if last_log else []
    nf = [_nfu_fio(x) for x in nf_raw][:200]

    return {
        "period": {"id": active_period.id, "name": active_period.name},
        "readings_without_debts": r_no_debts,
        "debts_without_readings": d_no_readings,
        "last_import_not_found": nf,
        "last_import_id": last_log.id if last_log else None,
    }
