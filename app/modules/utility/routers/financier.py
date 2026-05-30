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


@router.post(
    "/debts/preview-file",
    summary="Предпросмотр Excel-файла ОСВ 1С перед импортом",
)
async def debts_preview_file(
    account_type: str = Form(..., pattern="^(209|205)$"),
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Парсит Excel-файл БЕЗ сохранения в архив и БЕЗ создания DebtImportLog.
    Возвращает сводку: количество строк с ФИО, sum debt/overpayment, sample
    ФИО, file_hash (SHA256). Проверяет дубликат — если такой же файл уже
    был импортирован, возвращает ссылку на предыдущий лог.

    UI использует это для авто-проверки при выборе файла, чтобы:
      - не дать загрузить тот же файл дважды,
      - показать админу что он скармливает системе (счёт, период, суммы).
    """
    if current_user.role not in ("admin", "financier"):
        raise HTTPException(403, "Недостаточно прав")
    if not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(400, "Поддерживаются только Excel-файлы")

    header = await file.read(8)
    await file.seek(0)
    if not (header.startswith(b"PK\x03\x04") or header.startswith(b"\xd0\xcf\x11\xe0")):
        raise HTTPException(400, "Поддельное расширение или не Excel")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"Файл слишком большой ({len(content)/1024/1024:.1f} MB > {MAX_FILE_SIZE/1024/1024} MB)")

    # SHA256 хэш для дедупликации.
    import hashlib as _hl
    file_hash = _hl.sha256(content).hexdigest()

    # Парсим файл в памяти через BytesIO.
    import io as _io
    import openpyxl as _opx
    from decimal import Decimal as _D
    try:
        wb = _opx.load_workbook(filename=_io.BytesIO(content), read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        raise HTTPException(400, f"Не удалось открыть Excel: {e}")

    # Эвристика: что считаем «строкой с ФИО».
    def _looks_like_fio(s) -> bool:
        if not isinstance(s, str):
            return False
        s_norm = s.strip()
        if len(s_norm) < 6 or len(s_norm) > 80:
            return False
        # Минимум 2 заглавных слова кириллицы.
        words = s_norm.split()
        if len(words) < 2:
            return False
        good = sum(1 for w in words if w and w[0].isalpha() and w[0].isupper() and any('А' <= c <= 'я' or c == 'Ё' or c == 'ё' for c in w))
        if good < 2:
            return False
        # Не должно быть ключевых слов ОСВ.
        s_low = s_norm.lower()
        for kw in ("договор", "сальдо", "оборот", "итого", "период", "контрагент", "счёт", "счет"):
            if kw in s_low:
                return False
        return True

    rows_total = 0
    rows_with_fio = 0
    sample_fio = []
    sum_debt = _D("0")

    try:
        for row in ws.iter_rows(values_only=True):
            if not row:
                continue
            rows_total += 1
            fio_idx = None
            for col_idx, cell in enumerate(row):
                if _looks_like_fio(cell):
                    fio_idx = col_idx
                    break
            if fio_idx is None:
                continue
            rows_with_fio += 1
            if len(sample_fio) < 5:
                sample_fio.append(str(row[fio_idx]).strip())
            # Числа в строке — суммируем как debt (макс положительное) и
            # overpay (макс положительное). Это грубо, но даёт оценку.
            nums = []
            for cell in row[fio_idx + 1:]:
                if cell is None or cell == "":
                    continue
                try:
                    d = _D(str(cell).replace(",", "."))
                    if d != 0:
                        nums.append(d)
                except Exception:
                    pass
            # В ОСВ обычно последние 2 числа — финальное сальдо
            # (debt — Дебет, overpay — Кредит).
            if nums:
                # Простая эвристика: положительные большие → debt, остальные → overpay.
                # Точную классификацию даёт основной парсер; тут только превью.
                for n in nums:
                    if n > 0:
                        sum_debt += n / _D(len(nums))  # средняя — неточно, но не критично
    except Exception as e:
        wb.close()
        raise HTTPException(400, f"Ошибка парсинга: {e}")
    wb.close()

    # Проверка дубликата: проходим по последним 30 архивам, считаем их хэши.
    # Это медленно (читаем диски), но кешируем результат коротким TTL.
    duplicate_of = None
    recent_logs = (await db.execute(
        select(DebtImportLog)
        .where(DebtImportLog.archive_path.is_not(None))
        .order_by(desc(DebtImportLog.id))
        .limit(30)
    )).scalars().all()
    import os as _os
    for log in recent_logs:
        if not log.archive_path or not _os.path.exists(log.archive_path):
            continue
        try:
            with open(log.archive_path, "rb") as fh:
                arch_hash = _hl.sha256(fh.read()).hexdigest()
            if arch_hash == file_hash:
                duplicate_of = {
                    "log_id": log.id,
                    "account_type": log.account_type,
                    "started_at": log.started_at.isoformat() if log.started_at else None,
                    "status": log.status,
                }
                break
        except Exception:
            pass

    return {
        "file_name": file.filename,
        "size_bytes": len(content),
        "file_hash": file_hash,
        "duplicate_of": duplicate_of,
        "rows_total": rows_total,
        "rows_with_fio": rows_with_fio,
        "sample_fio": sample_fio,
        "expected_account_type": account_type,
    }


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
        has_data: bool = Query(False, description="Скрыть жильцов без данных из 1С (все 8 финансовых полей = 0)"),
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
    # Bug V: обороты для UI «движение средств».
    od209 = func.coalesce(func.sum(MeterReading.obor_debit_209), 0).label("obor_debit_209")
    oc209 = func.coalesce(func.sum(MeterReading.obor_credit_209), 0).label("obor_credit_209")
    od205 = func.coalesce(func.sum(MeterReading.obor_debit_205), 0).label("obor_debit_205")
    oc205 = func.coalesce(func.sum(MeterReading.obor_credit_205), 0).label("obor_credit_205")
    total = func.coalesce(func.sum(MeterReading.total_cost), 0).label("current_total_cost")

    stmt = select(
        User, Room, d209, o209, d205, o205, total,
        od209, oc209, od205, oc205,
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
    if has_data:
        # Bug AB: «не показывать пустых» — хотя бы одно из 8 финансовых
        # полей (сальдо + обороты по 209 и 205) > 0.
        stmt = stmt.having(
            (d209 + o209 + d205 + o205 + od209 + oc209 + od205 + oc205) > 0
        )

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

    # Для only_debtors/only_overpaid/min_debt/has_data count тоже надо
    # пересчитать через HAVING — делаем через subquery вместо дублирования.
    if only_debtors or only_overpaid or min_debt is not None or has_data:
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
            "current_total_cost": row[6],
            # Bug V: движение средств — обороты периода.
            "obor_debit_209": row[7],
            "obor_credit_209": row[8],
            "obor_debit_205": row[9],
            "obor_credit_205": row[10],
        })

    return {"total": total_items, "page": page, "size": limit, "items": items}


# =========================================================================
# ЗЕРКАЛО ПО КВАРТИРАМ: та же отчётность, но агрегация по ПОМЕЩЕНИЮ, без ФИО.
# Долг квартиры = сумма долгов всех жильцов комнаты (по room_id). Адрес вместо
# ФИО; кто живёт — в детализации /rooms/{id}/residents-finance.
# =========================================================================
@router.get("/rooms-status", summary="Список квартир с долгами (агрегация по помещению)")
async def get_rooms_with_debts(
        page: int = Query(1, ge=1),
        limit: int = Query(50, ge=1, le=500),
        search: str | None = Query(None),
        only_debtors: bool = Query(False),
        only_overpaid: bool = Query(False),
        has_data: bool = Query(False),
        dormitory: Optional[str] = Query(None),
        place_type: Optional[str] = Query(None, pattern="^(dormitory|house)$"),
        min_debt: Optional[float] = Query(None, ge=0),
        sort_by: str = Query("room", pattern="^(room|debt|overpay|total)$"),
        sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    if current_user.role not in ("financier", "accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    offset = (page - 1) * limit
    active = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    period_id = active.id if active else None

    d209 = func.coalesce(func.sum(MeterReading.debt_209), 0).label("debt_209")
    o209 = func.coalesce(func.sum(MeterReading.overpayment_209), 0).label("overpayment_209")
    d205 = func.coalesce(func.sum(MeterReading.debt_205), 0).label("debt_205")
    o205 = func.coalesce(func.sum(MeterReading.overpayment_205), 0).label("overpayment_205")
    od209 = func.coalesce(func.sum(MeterReading.obor_debit_209), 0).label("obor_debit_209")
    oc209 = func.coalesce(func.sum(MeterReading.obor_credit_209), 0).label("obor_credit_209")
    od205 = func.coalesce(func.sum(MeterReading.obor_debit_205), 0).label("obor_debit_205")
    oc205 = func.coalesce(func.sum(MeterReading.obor_credit_205), 0).label("obor_credit_205")
    total = func.coalesce(func.sum(MeterReading.total_cost), 0).label("current_total_cost")
    residents = func.count(func.distinct(MeterReading.user_id)).label("residents_count")

    stmt = select(
        Room, d209, o209, d205, o205, total, od209, oc209, od205, oc205, residents,
    ).outerjoin(
        MeterReading,
        (Room.id == MeterReading.room_id) & (MeterReading.period_id == period_id),
    )
    search_condition = None
    if search:
        sv = f"%{search.lower()}%"
        search_condition = or_(
            func.lower(Room.dormitory_name).like(sv),
            func.lower(Room.room_number).like(sv),
            func.lower(Room.street).like(sv),
            func.lower(Room.house_number).like(sv),
            func.lower(Room.apartment_number).like(sv),
        )
        stmt = stmt.where(search_condition)
    if dormitory:
        stmt = stmt.where(Room.dormitory_name == dormitory)
    if place_type:
        stmt = stmt.where(Room.place_type == place_type)
    stmt = stmt.group_by(Room.id)
    if only_debtors:
        stmt = stmt.having((d209 + d205) > 0)
    if only_overpaid:
        stmt = stmt.having((o209 + o205) > 0)
    if min_debt is not None:
        stmt = stmt.having((d209 + d205) >= min_debt)
    if has_data:
        stmt = stmt.having(
            (d209 + o209 + d205 + o205 + od209 + oc209 + od205 + oc205) > 0
        )

    sort_map = {
        "room": (Room.dormitory_name, Room.room_number, Room.street, Room.house_number),
        "debt": ((d209 + d205).label("__d"),),
        "overpay": ((o209 + o205).label("__o"),),
        "total": (total,),
    }
    direction = desc if sort_dir == "desc" else asc
    order_cols = [direction(c).nulls_last() for c in sort_map[sort_by]]
    order_cols.append(asc(Room.id))
    stmt = stmt.order_by(*order_cols).limit(limit).offset(offset)

    count_stmt = select(func.count(Room.id))
    if search_condition is not None:
        count_stmt = count_stmt.where(search_condition)
    if dormitory:
        count_stmt = count_stmt.where(Room.dormitory_name == dormitory)
    if place_type:
        count_stmt = count_stmt.where(Room.place_type == place_type)
    if only_debtors or only_overpaid or min_debt is not None or has_data:
        inner = stmt.with_only_columns(Room.id).limit(None).offset(None).order_by(None).subquery()
        count_stmt = select(func.count()).select_from(inner)
    total_items = (await db.execute(count_stmt)).scalar_one()

    rows = (await db.execute(stmt)).all()
    items = []
    for row in rows:
        room = row[0]
        items.append({
            "room_id": room.id,
            "address": room.format_address,
            "place_type": room.place_type,
            "dormitory_name": room.dormitory_name,
            "room_number": room.room_number,
            "residents_count": int(row[10] or 0),
            "debt_209": row[1], "overpayment_209": row[2],
            "debt_205": row[3], "overpayment_205": row[4],
            "current_total_cost": row[5],
            "obor_debit_209": row[6], "obor_credit_209": row[7],
            "obor_debit_205": row[8], "obor_credit_205": row[9],
        })
    return {"total": total_items, "page": page, "size": limit, "items": items}


@router.get("/rooms/{room_id}/residents-finance",
            summary="Финансы жильцов конкретной квартиры (раскрытие строки)")
async def room_residents_finance(
        room_id: int,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
):
    if current_user.role not in ("financier", "accountant", "admin"):
        raise HTTPException(status_code=403, detail="Доступ запрещен")
    active = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    period_id = active.id if active else None

    d209 = func.coalesce(func.sum(MeterReading.debt_209), 0)
    o209 = func.coalesce(func.sum(MeterReading.overpayment_209), 0)
    d205 = func.coalesce(func.sum(MeterReading.debt_205), 0)
    o205 = func.coalesce(func.sum(MeterReading.overpayment_205), 0)
    total = func.coalesce(func.sum(MeterReading.total_cost), 0)

    stmt = select(User, d209, o209, d205, o205, total).outerjoin(
        MeterReading,
        (User.id == MeterReading.user_id) & (MeterReading.period_id == period_id),
    ).where(
        User.room_id == room_id, User.is_deleted.is_(False),
    ).group_by(User.id).order_by(User.username)
    rows = (await db.execute(stmt)).all()
    return {
        "room_id": room_id,
        "residents": [{
            "user_id": r[0].id,
            "username": r[0].username,
            "full_name": getattr(r[0], "full_name", None),
            "debt_209": r[1], "overpayment_209": r[2],
            "debt_205": r[3], "overpayment_205": r[4],
            "current_total_cost": r[5],
        } for r in rows],
    }


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

    # --- Учёт по КВАРТИРАМ (помещениям), а не по жильцам ---
    # Квартир с долгом: distinct room_id где сумма debt_209+205 > 0.
    rooms_debt_q = (
        select(func.count(func.distinct(MeterReading.room_id)))
        .where(
            MeterReading.period_id == period_id,
            MeterReading.room_id.isnot(None),
            (MeterReading.debt_209 > 0) | (MeterReading.debt_205 > 0),
        )
    )
    rooms_with_debt_count = (await db.execute(rooms_debt_q)).scalar_one()

    # Квартир с переплатой
    rooms_over_q = (
        select(func.count(func.distinct(MeterReading.room_id)))
        .where(
            MeterReading.period_id == period_id,
            MeterReading.room_id.isnot(None),
            (MeterReading.overpayment_209 > 0) | (MeterReading.overpayment_205 > 0),
        )
    )
    rooms_overpaying_count = (await db.execute(rooms_over_q)).scalar_one()

    # Всего квартир с данными в периоде (для шапки в режиме «Квартиры»)
    total_rooms_q = (
        select(func.count(func.distinct(MeterReading.room_id)))
        .where(MeterReading.period_id == period_id, MeterReading.room_id.isnot(None))
    )
    total_rooms = (await db.execute(total_rooms_q)).scalar_one()

    # Всего активных жильцов
    total_users_q = select(func.count(User.id)).where(
        User.is_deleted.is_(False), User.role == "user",
    )
    total_users = (await db.execute(total_users_q)).scalar_one()

    total_debt = float(total_debt_209 or 0) + float(total_debt_205 or 0)
    total_over = float(total_over_209 or 0) + float(total_over_205 or 0)
    avg_debt = (total_debt / debtors_count) if debtors_count else 0.0
    avg_debt_room = (total_debt / rooms_with_debt_count) if rooms_with_debt_count else 0.0

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
        # Учёт по квартирам (помещениям)
        "rooms_with_debt_count": int(rooms_with_debt_count or 0),
        "rooms_overpaying_count": int(rooms_overpaying_count or 0),
        "total_rooms": int(total_rooms or 0),
        "avg_debt_per_room": round(avg_debt_room, 2),
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
        # housing_001/E2-A: для дома колонка "Общежитие" заполняется
        # улицей+номером дома, "Комната" — номером квартиры. Для общаги
        # сохраняем старое поведение (dormitory_name + room_number).
        if room and room.place_type == "house":
            _addr = ", ".join(filter(None, [
                f"ул. {room.street}" if room.street else None,
                f"д. {room.house_number}" if room.house_number else None,
            ])) or ""
            ws.cell(row=i, column=3, value=_addr)
            ws.cell(row=i, column=4, value=(f"кв. {room.apartment_number}" if room.apartment_number else ""))
        else:
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

    # Bug AG: applied_state теперь keyed by user_id (раньше room_id — в
    # коммуналке два жильца перезаписывали друг друга).
    all_user_ids = set(cur_state.keys()) | set(prev_state.keys())
    for user_id_str in all_user_ids:
        cur = cur_state.get(user_id_str, {})
        prev = prev_state.get(user_id_str, {})
        cur_debt = _dec(cur, debt_key)
        prev_debt = _dec(prev, debt_key)
        cur_over = _dec(cur, over_key)
        prev_over = _dec(prev, over_key)

        # Метаданные берём из cur если есть, иначе из prev (если жилец исчез)
        meta_username = cur.get("username") or prev.get("username") or "—"
        meta_room = cur.get("room_label") or prev.get("room_label") or "—"
        # room_id хранится внутри applied_state (после Bug AG) либо берём
        # из cur/prev. Для legacy-логов до Bug AG в applied_state нет user_id,
        # вместо него стоит room_id — попробуем привести к int безопасно.
        room_id_val = cur.get("room_id") or prev.get("room_id")
        try:
            user_id_int = int(user_id_str)
        except Exception:
            user_id_int = None

        if cur_debt > prev_debt:
            entry = {
                "user_id": user_id_int,
                "room_id": room_id_val,
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
                "user_id": user_id_int,
                "room_id": room_id_val,
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
                "user_id": user_id_int,
                "room_id": room_id_val,
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
    completed-импорт — debt+overpayment по этому юзеру.

    Bug AG: applied_state теперь keyed by user_id, поэтому переезды
    больше не теряют точки (раньше при смене комнаты история обрывалась).
    UI рисует две линии: 209 (коммунальный) и 205 (найм), плюс tabular
    разрез по каждому импорту.
    """
    _require_finance(current_user)

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Жилец не найден")

    user_id_key = str(user.id)

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
        entry = st.get(user_id_key)
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


@router.post("/debts/import-history/{log_id}/reparse",
             summary="Переимпорт лога 1С из архива с актуальной логикой парсера")
async def debts_reparse(
    log_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bug AE: Reading'и, импортированные до Bug U-fix6, имеют
    debt_209/debt_205 = начальное сальдо вместо конечного (когда обороты
    Кредит погасили долг, а end-колонки в ОСВ пустые). Парсер обновлён
    (pick_saldo_pair учитывает обороты), но сами reading'и в БД не
    пересчитаны автоматически — там лежат старые значения.

    Этот endpoint берёт archive_path лога и заново запускает
    import_debts_task: pipeline UPDATE-ит существующие reading'и
    значениями из актуальной логики парсинга. Новый DebtImportLog
    создаётся (для аудита и возможного отката).

    Что НЕ делает:
      - не удаляет старый log (audit trail сохраняется)
      - не trigger'ит revert старого (snapshot_data старого остаётся
        корректным относительно того момента, отдельная история)
    """
    _require_finance(current_user)
    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог импорта не найден")
    if not log.archive_path:
        raise HTTPException(
            404,
            "Архив этого импорта не сохранён (старый импорт до миграции debts_002). "
            "Загрузите тот же файл из 1С вручную через форму импорта.",
        )
    if not os.path.exists(log.archive_path):
        raise HTTPException(
            404,
            "Файл архива физически удалён (retention-policy / ручная очистка). "
            "Загрузите тот же файл из 1С вручную через форму импорта.",
        )

    import uuid as _uuid
    batch_id = str(_uuid.uuid4())
    task = import_debts_task.delay(
        log.archive_path,
        log.account_type,
        started_by_id=current_user.id,
        started_by_username=current_user.username,
        batch_id=batch_id,
        original_file_name=log.file_name or f"reparse_{log.account_type}_{log.id}.xlsx",
    )

    logger.info(
        f"[REPARSE] log_id={log_id} account={log.account_type} "
        f"archive={log.archive_path} task={task.id} batch={batch_id}"
    )

    return {
        "task_id": task.id,
        "status": "processing",
        "account_type": log.account_type,
        "batch_id": batch_id,
        "source_log_id": log_id,
        "source_file": log.file_name,
    }


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


@router.get(
    "/debts/check-resident-coverage/{user_id}",
    summary="Найти жильца в архивах последних импортов 1С (диагностика)",
)
async def debts_check_resident_coverage(
    user_id: int,
    last_n: int = 10,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Для конкретного жильца перебирает последние N импортов 1С,
    парсит архивные xlsx, ищет ФИО (точное совпадение + substring).
    Полезно для диагностики «почему у Миронова нет долгов»:
      - если в архивах есть с цифрами → fuzzy-привязка ошиблась, нужен reassign
      - если есть с нулями → нормально, нет долга
      - если нет вообще → жильца не передавали из 1С
    """
    if current_user.role not in ("admin", "financier"):
        raise HTTPException(403, "Недостаточно прав")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Жилец не найден")

    fio_db = (user.full_name or user.username or "").strip()
    if not fio_db:
        raise HTTPException(400, "У жильца нет ФИО — нечего искать")

    # Нормализация для substring-сравнения (нижний регистр, без точек,
    # без двойных пробелов).
    import re as _re
    def _norm(s: str) -> str:
        s = (s or "").lower().replace(".", " ").replace(",", " ")
        s = _re.sub(r"\s+", " ", s).strip()
        return s

    fio_db_norm = _norm(fio_db)
    # Также берём фамилию + первую букву имени для substring-поиска.
    parts = fio_db_norm.split()
    surname = parts[0] if parts else ""

    logs = (await db.execute(
        select(DebtImportLog)
        .where(
            DebtImportLog.archive_path.is_not(None),
            DebtImportLog.status.in_(["completed", "reverted"]),
        )
        .order_by(desc(DebtImportLog.id))
        .limit(last_n)
    )).scalars().all()

    import openpyxl as _opx
    import os as _os
    from decimal import Decimal as _D
    results = []
    for log in logs:
        item = {
            "log_id": log.id,
            "account_type": log.account_type,
            "started_at": log.started_at.isoformat() if log.started_at else None,
            "status": log.status,
            "matches": [],
            "error": None,
        }
        try:
            if not _os.path.exists(log.archive_path):
                item["error"] = "archive_missing"
                results.append(item)
                continue
            wb = _opx.load_workbook(filename=log.archive_path, read_only=True, data_only=True)
            ws = wb.active
            # ФИО в ОСВ 1С может быть в любой строковой колонке (зависит
            # от шаблона выгрузки). Ищем substring фамилии во ВСЕХ
            # колонках строки.
            for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if not row:
                    continue
                # Ищем колонку с ФИО (substring "фамилия" в строковом значении).
                fio_cell = None
                fio_col_idx = None
                for col_idx, cell_val in enumerate(row):
                    if cell_val is None or not isinstance(cell_val, str):
                        continue
                    cell_norm = _norm(cell_val)
                    if not cell_norm or not surname:
                        continue
                    if surname not in cell_norm:
                        continue
                    # Sanity: ячейка должна выглядеть как ФИО (несколько слов
                    # с заглавных букв), а не как «Договор...» или «Сальдо...».
                    # Фильтруем явные ключевые слова ОСВ.
                    if any(kw in cell_norm for kw in [
                        "договор", "сальдо", "оборот", "итого", "период",
                        "квартир", "общежит", "счёт", "счет", "помещен",
                    ]):
                        continue
                    fio_cell = cell_val
                    fio_col_idx = col_idx
                    break
                if fio_cell is None:
                    continue
                fio_cell_norm = _norm(str(fio_cell))
                # Собираем числовые значения из строки (после колонки ФИО),
                # чтобы показать сальдо.
                numeric_cols = []
                for col_val in row[fio_col_idx + 1:]:
                    if col_val is None or col_val == "":
                        continue
                    try:
                        d = _D(str(col_val).replace(",", "."))
                        if d != 0:
                            numeric_cols.append(float(d))
                    except Exception:
                        pass
                exact = fio_cell_norm == fio_db_norm
                item["matches"].append({
                    "row_excel": row_idx,
                    "col_excel": fio_col_idx + 1,  # 1-based для удобства админа
                    "fio_in_excel": str(fio_cell).strip(),
                    "exact_match": exact,
                    "numeric_values": numeric_cols[:6],
                })
            wb.close()
        except Exception as exc:
            item["error"] = f"parse_failed: {exc}"
        results.append(item)

    return {
        "user_id": user_id,
        "fio_db": fio_db,
        "imports_checked": len(logs),
        "results": results,
    }


@router.get(
    "/debts/import-history/{log_id}/parser-diagnose",
    summary="Диагностика парсера: какие колонки нашёл, что извлёк",
)
async def debts_parser_diagnose(
    log_id: int,
    fio_search: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Прогоняет логику парсера заголовков на архивном файле этого импорта
    и возвращает: какие колонки нашёл (debt_first/last, overpay_first/last),
    где была account-total row (209.34 / 205.X), какие numeric_positions
    в ней, sample 3 строк жильцов с распарсенными debt/over.

    Этот endpoint **не** делает импорт — только показывает что парсер
    видит. Помогает диагностировать «почему у Бендаса всё ещё 2385.07».
    """
    if current_user.role not in ("admin", "financier"):
        raise HTTPException(403, "Недостаточно прав")

    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог не найден")
    if not log.archive_path:
        raise HTTPException(400, "У этого импорта нет архива (старый импорт без archive_path)")

    import os as _os
    if not _os.path.exists(log.archive_path):
        raise HTTPException(404, f"Архив не найден на диске: {log.archive_path}")

    # Используем тот же парсер что и в основном импорте — копируем сюда
    # ключевые шаги.
    import openpyxl as _opx
    from app.modules.utility.services.debt_import import clean_decimal, pick_saldo_pair
    try:
        ws = _opx.load_workbook(filename=log.archive_path, read_only=True, data_only=True).active
    except Exception as e:
        raise HTTPException(400, f"openpyxl не открыл файл: {e}")

    section_markers: dict = {}
    debit_cols: list = []
    credit_cols: list = []
    account_total = None  # {row_idx, label_col, label, numeric_positions, all_values}

    for row_idx, row in enumerate(ws.iter_rows(min_row=1, max_row=20, values_only=True), start=1):
        if not row:
            continue
        # Section markers + debit/credit positions
        for col_idx, cell in enumerate(row):
            if cell is None or not isinstance(cell, str):
                continue
            cell_norm = cell.strip().lower()
            if not cell_norm:
                continue
            if "сальдо" in cell_norm and "начал" in cell_norm and "начало" not in section_markers:
                section_markers["начало"] = col_idx
            elif "оборот" in cell_norm and ("период" in cell_norm or len(cell_norm) < 30) and "обороты" not in section_markers:
                section_markers["обороты"] = col_idx
            elif "сальдо" in cell_norm and "конец" in cell_norm and "конец" not in section_markers:
                section_markers["конец"] = col_idx
            elif cell_norm == "дебет":
                debit_cols.append(col_idx)
            elif cell_norm == "кредит":
                credit_cols.append(col_idx)
        # Account total row
        if account_total is None:
            for col_label in range(min(3, len(row))):
                cell = row[col_label]
                if cell is None:
                    continue
                s = str(cell).strip()
                if s.startswith("209.") or s.startswith("205.") or s == "209" or s == "205":
                    numeric_positions = []
                    all_values = {}
                    for col_idx in range(col_label + 1, len(row)):
                        c = row[col_idx]
                        if c is None or c == "":
                            continue
                        try:
                            d = clean_decimal(c)
                            if d != 0:
                                numeric_positions.append(col_idx)
                                all_values[col_idx] = float(d)
                        except Exception:
                            pass
                    account_total = {
                        "row_idx": row_idx,
                        "label_col": col_label,
                        "label": s,
                        "numeric_positions": numeric_positions,
                        "all_values": all_values,
                    }
                    break

    debit_cols = sorted(set(debit_cols))
    credit_cols = sorted(set(credit_cols))

    # Какие колонки выберет парсер
    chosen = {
        "debt_col_first": None,
        "debt_col_last": None,
        "overpay_col_first": None,
        "overpay_col_last": None,
        "obor_debit_col": None,
        "obor_credit_col": None,
        "strategy": None,
    }
    if account_total and len(account_total["numeric_positions"]) >= 4:
        np_list = account_total["numeric_positions"]
        chosen["debt_col_first"] = np_list[0]
        chosen["overpay_col_first"] = np_list[1] if len(np_list) > 1 else np_list[0]
        if len(np_list) >= 6:
            chosen["obor_debit_col"] = np_list[2]
            chosen["obor_credit_col"] = np_list[3]
            chosen["debt_col_last"] = np_list[4]
            chosen["overpay_col_last"] = np_list[5]
        elif len(np_list) == 5:
            chosen["obor_debit_col"] = np_list[2]
            chosen["debt_col_last"] = np_list[3]
            chosen["overpay_col_last"] = np_list[4]
        elif len(np_list) == 4:
            chosen["debt_col_last"] = np_list[2]
            chosen["overpay_col_last"] = np_list[3]
        chosen["strategy"] = "0_account_total_row"

    # Sample жильцов: для каждого извлекаем debt/over через pick_saldo_pair.
    # Если fio_search задан — ищем только этого жильца (substring match).
    # Иначе — первые 3 жильца как preview.
    samples = []
    search_norm = (fio_search or "").strip().lower()
    if chosen["debt_col_last"] is not None and chosen["overpay_col_last"] is not None:
        count = 0
        max_count = 50 if search_norm else 3
        for row in ws.iter_rows(min_row=10, max_row=2000, values_only=True):
            if count >= max_count or not row:
                continue
            # Ищем ФИО в первых 5 колонках
            fio = None
            fio_col = None
            for col_idx in range(min(5, len(row))):
                cell = row[col_idx]
                if not isinstance(cell, str):
                    continue
                s = str(cell).strip()
                if " " in s and len(s.split()) >= 2 and any('А' <= c <= 'я' for c in s):
                    # Sanity: не "Договор", не "Сальдо", не "Контрагенты"
                    s_low = s.lower()
                    if any(kw in s_low for kw in ["договор", "сальдо", "оборот", "итого", "контрагент", "счёт", "счет", "помещен", "период"]):
                        continue
                    fio = s
                    fio_col = col_idx
                    break
            if not fio:
                continue
            # Если задан поиск — фильтруем по substring.
            if search_norm and search_norm not in fio.lower():
                continue
            try:
                debt, over = pick_saldo_pair(
                    row,
                    end_debit_col=chosen["debt_col_last"],
                    end_credit_col=chosen["overpay_col_last"],
                    start_debit_col=chosen["debt_col_first"],
                    start_credit_col=chosen["overpay_col_first"],
                    obor_debit_col=chosen["obor_debit_col"],
                    obor_credit_col=chosen["obor_credit_col"],
                )
            except Exception:
                debt, over = 0, 0

            # Raw values в каждой ключевой колонке — для понимания структуры.
            def _raw(col):
                if col is None or col >= len(row):
                    return None
                v = row[col]
                if v is None or v == "":
                    return None
                if isinstance(v, (int, float)):
                    return float(v)
                try:
                    return float(clean_decimal(v))
                except Exception:
                    return str(v)

            sample = {
                "fio": fio,
                "fio_col": fio_col,
                "debt_extracted": float(debt),
                "overpayment_extracted": float(over),
                "raw_values": {
                    f"col{chosen['debt_col_first']}_start_debit": _raw(chosen["debt_col_first"]),
                    f"col{chosen['overpay_col_first']}_start_credit": _raw(chosen["overpay_col_first"]),
                    **({f"col{chosen['obor_debit_col']}_obor_debit": _raw(chosen["obor_debit_col"])} if chosen.get("obor_debit_col") is not None else {}),
                    **({f"col{chosen['obor_credit_col']}_obor_credit": _raw(chosen["obor_credit_col"])} if chosen.get("obor_credit_col") is not None else {}),
                    f"col{chosen['debt_col_last']}_end_debit": _raw(chosen["debt_col_last"]),
                    f"col{chosen['overpay_col_last']}_end_credit": _raw(chosen["overpay_col_last"]),
                },
            }

            # Если поиск конкретного жильца — добавляем сравнение с БД.
            if search_norm:
                # Ищем жильца в БД через нормализацию.
                from app.modules.utility.services.debt_import import normalize_name
                from rapidfuzz import process, fuzz
                norm = normalize_name(fio)
                # Загружаем кэш жильцов.
                users_all = (await db.execute(
                    select(User).where(User.is_deleted.is_(False))
                )).scalars().all()
                user_map = {normalize_name(u.username): u for u in users_all}
                matched_user = user_map.get(norm)
                fuzzy_match_info = None
                if not matched_user:
                    # Fuzzy
                    match = process.extractOne(
                        norm, list(user_map.keys()),
                        scorer=fuzz.token_sort_ratio,
                    )
                    if match:
                        best_key, score, _ = match
                        if score >= 80:
                            matched_user = user_map[best_key]
                            fuzzy_match_info = {"key": best_key, "score": score}
                        else:
                            fuzzy_match_info = {"key": best_key, "score": score, "too_low": True}

                sample["db_lookup"] = None
                if matched_user:
                    # Загружаем текущие debt/over из активного period.
                    active_period = (await db.execute(
                        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
                    )).scalars().first()
                    db_reading = None
                    if active_period:
                        db_reading = (await db.execute(
                            select(MeterReading).where(
                                MeterReading.user_id == matched_user.id,
                                MeterReading.period_id == active_period.id,
                            ).limit(1)
                        )).scalars().first()
                    is_account_209 = log.account_type == "209"
                    sample["db_lookup"] = {
                        "matched_user_id": matched_user.id,
                        "matched_username": matched_user.username,
                        "matched_full_name": matched_user.full_name,
                        "fuzzy": fuzzy_match_info,
                        "db_debt": float(db_reading.debt_209 if is_account_209 else db_reading.debt_205) if db_reading else None,
                        "db_overpayment": float(db_reading.overpayment_209 if is_account_209 else db_reading.overpayment_205) if db_reading else None,
                        "expected_debt": float(debt),
                        "expected_overpayment": float(over),
                        "mismatch": (db_reading is None) or (
                            abs(float(debt) - float(db_reading.debt_209 if is_account_209 else db_reading.debt_205 or 0)) > 0.01
                        ),
                    }
                else:
                    sample["db_lookup"] = {
                        "matched_user_id": None,
                        "fuzzy": fuzzy_match_info,
                        "expected_debt": float(debt),
                        "expected_overpayment": float(over),
                        "mismatch": True,
                        "reason": "user_not_found_in_db",
                    }
            samples.append(sample)
            count += 1

    ws.parent.close()

    return {
        "log_id": log_id,
        "archive_path": log.archive_path,
        "section_markers": section_markers,
        "debit_cols_in_header": debit_cols,
        "credit_cols_in_header": credit_cols,
        "account_total": account_total,
        "chosen": chosen,
        "samples": samples,
    }


@router.post(
    "/debts/probe-update/{user_id}",
    summary="Bug AF: проверить, что UPDATE на reading жильца реально доходит до БД",
)
async def debts_probe_update(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bug AF probe: пробует UPDATE двумя стратегиями (только по `id` —
    как сейчас в импорте; и по `(id, created_at)` — кандидат на фикс
    партиционирования) и показывает rowcount/значение после каждой.

    БЕЗОПАСНО: в конце делает rollback — БД не меняется. Используется
    как «истина в последней инстанции» — если UPDATE по `id` возвращает
    rowcount=0, это партиционная проблема (composite PK + RANGE BY
    created_at). Если rowcount=1, но value не меняется — что-то другое.
    """
    _require_finance(current_user)

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    if not active_period:
        raise HTTPException(404, "Нет активного периода")

    reading = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user_id,
            MeterReading.period_id == active_period.id,
        ).limit(1)
    )).scalars().first()
    if not reading:
        raise HTTPException(404, f"Reading для user_id={user_id} в активном периоде не найден")

    from sqlalchemy import update as _sa_update, text as _sa_text

    reading_id = reading.id
    created_at = reading.created_at
    debt_before = float(reading.debt_209 or 0)

    # Стратегия A: UPDATE по id (как сейчас в импорте).
    res_a = await db.execute(
        _sa_update(MeterReading)
        .where(MeterReading.id == reading_id)
        .values(debt_209=0)
    )
    rowcount_a = res_a.rowcount or 0
    val_a = (await db.execute(
        _sa_text("SELECT debt_209 FROM readings WHERE id = :rid AND created_at = :ca"),
        {"rid": reading_id, "ca": created_at},
    )).scalar()

    # Откатываем A перед B — чистый эксперимент.
    await db.rollback()

    # Стратегия B: UPDATE по (id, created_at).
    res_b = await db.execute(
        _sa_update(MeterReading)
        .where(MeterReading.id == reading_id)
        .where(MeterReading.created_at == created_at)
        .values(debt_209=0)
    )
    rowcount_b = res_b.rowcount or 0
    val_b = (await db.execute(
        _sa_text("SELECT debt_209 FROM readings WHERE id = :rid AND created_at = :ca"),
        {"rid": reading_id, "ca": created_at},
    )).scalar()

    # Стратегия C: raw SQL — на случай если ORM что-то ломает.
    await db.rollback()
    res_c = await db.execute(
        _sa_text(
            "UPDATE readings SET debt_209 = 0 "
            "WHERE id = :rid AND created_at = :ca"
        ),
        {"rid": reading_id, "ca": created_at},
    )
    rowcount_c = res_c.rowcount or 0
    val_c = (await db.execute(
        _sa_text("SELECT debt_209 FROM readings WHERE id = :rid AND created_at = :ca"),
        {"rid": reading_id, "ca": created_at},
    )).scalar()

    # Финальный rollback — никаких изменений в БД.
    await db.rollback()

    return {
        "user_id": user_id,
        "reading_id": reading_id,
        "created_at": created_at.isoformat() if created_at else None,
        "debt_209_before": debt_before,
        "strategies": {
            "A_orm_by_id_only": {
                "rowcount": rowcount_a,
                "value_after_in_db": float(val_a) if val_a is not None else None,
                "worked": (rowcount_a == 1 and val_a == 0),
            },
            "B_orm_by_id_and_created_at": {
                "rowcount": rowcount_b,
                "value_after_in_db": float(val_b) if val_b is not None else None,
                "worked": (rowcount_b == 1 and val_b == 0),
            },
            "C_raw_sql_by_id_and_created_at": {
                "rowcount": rowcount_c,
                "value_after_in_db": float(val_c) if val_c is not None else None,
                "worked": (rowcount_c == 1 and val_c == 0),
            },
        },
        "diagnosis": (
            "partitioning_blocks_update" if not (rowcount_a == 1) and rowcount_b == 1
            else "orm_issue" if rowcount_b != 1 and rowcount_c == 1
            else "all_work_check_other_writer" if rowcount_a == 1
            else "all_fail_deeper_problem"
        ),
        "note": "Все стратегии в конце откатываются — БД не изменена.",
    }


@router.get(
    "/debts/integrity-check",
    summary="Анализатор: сравнить applied_state свежего импорта с БД (Этап 2)",
)
async def debts_integrity_check(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Этап 2: проверка целостности долгов в активном периоде.

    Сравнивает что **должно** быть (по applied_state последних 209 и 205
    импортов) с тем, что **есть** в `readings.debt_*`. Три категории
    проблем:

      1) **drift** — applied_state[u] и reading[u] оба есть, но debt
         различается > 1₽. Симптом: что-то перезаписало после импорта
         (manual_receipt, recalc, ручная правка).
      2) **missing_in_db** — applied_state ожидает долг у юзера, а
         reading'а у него вообще нет. Симптом: импорт не дошёл, или
         reading удалён вручную.
      3) **extra_in_db** — reading с долгом есть, в applied_state юзера
         нет. Симптом: zombie от старого Bug AG (см. /debts/zombie-readings).

    Read-only. Auto-fix не делает (на каждую категорию — свой инструмент:
    drift → reparse, missing → reparse, extra → cleanup-zombie-readings).
    """
    _require_finance(current_user)

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    if not active_period:
        raise HTTPException(404, "Нет активного периода")
    period_id = active_period.id

    async def _latest_applied(acct: str):
        log = (await db.execute(
            select(DebtImportLog)
            .where(
                DebtImportLog.account_type == acct,
                DebtImportLog.status == "completed",
                DebtImportLog.applied_state.is_not(None),
            )
            .order_by(DebtImportLog.id.desc()).limit(1)
        )).scalars().first()
        return log

    log_209 = await _latest_applied("209")
    log_205 = await _latest_applied("205")
    state_209 = (log_209.applied_state or {}) if log_209 else {}
    state_205 = (log_205.applied_state or {}) if log_205 else {}

    # Объединяем expected_state по юзерам: для каждого user_id — ожидаемые debt_209/205.
    expected: dict[int, dict] = {}
    for uid_str, vals in state_209.items():
        try:
            uid = int(uid_str)
        except Exception:
            continue
        expected.setdefault(uid, {"username": None, "room_label": None})
        expected[uid]["debt_209"] = float(vals.get("debt_209", "0") or 0)
        expected[uid]["overpayment_209"] = float(vals.get("overpayment_209", "0") or 0)
        expected[uid]["username"] = vals.get("username")
        expected[uid]["room_label"] = vals.get("room_label")
    for uid_str, vals in state_205.items():
        try:
            uid = int(uid_str)
        except Exception:
            continue
        expected.setdefault(uid, {"username": None, "room_label": None})
        expected[uid]["debt_205"] = float(vals.get("debt_205", "0") or 0)
        expected[uid]["overpayment_205"] = float(vals.get("overpayment_205", "0") or 0)
        if not expected[uid].get("username"):
            expected[uid]["username"] = vals.get("username")
        if not expected[uid].get("room_label"):
            expected[uid]["room_label"] = vals.get("room_label")

    # Все reading'и активного периода (одной выборкой).
    readings = (await db.execute(
        select(MeterReading).where(MeterReading.period_id == period_id)
    )).scalars().all()
    readings_by_user: dict[int, "MeterReading"] = {}
    for r in readings:
        if r.user_id is not None:
            readings_by_user[r.user_id] = r

    drift = []
    missing_in_db = []
    extra_in_db = []

    THR = 1.0  # порог расхождения в рублях

    # 1+2: сверяем expected → reality
    for uid, exp in expected.items():
        r = readings_by_user.get(uid)
        exp_d209 = exp.get("debt_209", 0.0)
        exp_o209 = exp.get("overpayment_209", 0.0)
        exp_d205 = exp.get("debt_205", 0.0)
        exp_o205 = exp.get("overpayment_205", 0.0)
        if r is None:
            # missing — только если ожидалось ненулевое сальдо
            if max(exp_d209, exp_o209, exp_d205, exp_o205) > THR:
                missing_in_db.append({
                    "user_id": uid,
                    "username": exp.get("username"),
                    "room_label": exp.get("room_label"),
                    "expected": {
                        "debt_209": exp_d209, "overpayment_209": exp_o209,
                        "debt_205": exp_d205, "overpayment_205": exp_o205,
                    },
                })
            continue

        actual_d209 = float(r.debt_209 or 0)
        actual_o209 = float(r.overpayment_209 or 0)
        actual_d205 = float(r.debt_205 or 0)
        actual_o205 = float(r.overpayment_205 or 0)

        diff_d209 = actual_d209 - exp_d209
        diff_o209 = actual_o209 - exp_o209
        diff_d205 = actual_d205 - exp_d205
        diff_o205 = actual_o205 - exp_o205
        max_abs_diff = max(abs(diff_d209), abs(diff_o209), abs(diff_d205), abs(diff_o205))
        if max_abs_diff > THR:
            drift.append({
                "user_id": uid,
                "reading_id": r.id,
                "username": exp.get("username"),
                "room_label": exp.get("room_label"),
                "expected": {
                    "debt_209": exp_d209, "overpayment_209": exp_o209,
                    "debt_205": exp_d205, "overpayment_205": exp_o205,
                },
                "actual": {
                    "debt_209": actual_d209, "overpayment_209": actual_o209,
                    "debt_205": actual_d205, "overpayment_205": actual_o205,
                },
                "max_abs_diff": max_abs_diff,
            })

    # 3: reading'и, которых нет в expected (zombie)
    known = set(expected.keys())
    user_ids_for_rooms = set()
    for r in readings:
        if r.user_id is None:
            continue
        if r.user_id in known:
            continue
        has_money = (
            float(r.debt_209 or 0) > THR or float(r.debt_205 or 0) > THR
            or float(r.overpayment_209 or 0) > THR or float(r.overpayment_205 or 0) > THR
        )
        if not has_money:
            continue
        user_ids_for_rooms.add(r.user_id)

    # Загружаем username/комнаты для zombie batch'ем
    extra_users_map = {}
    extra_rooms_map = {}
    if user_ids_for_rooms:
        u_rows = (await db.execute(
            select(User).where(User.id.in_(user_ids_for_rooms))
        )).scalars().all()
        extra_users_map = {u.id: u for u in u_rows}
        rids = {r.room_id for r in readings if r.user_id in user_ids_for_rooms and r.room_id}
        if rids:
            r_rows = (await db.execute(
                select(Room).where(Room.id.in_(rids))
            )).scalars().all()
            extra_rooms_map = {rm.id: rm for rm in r_rows}

    for r in readings:
        if r.user_id is None or r.user_id in known:
            continue
        has_money = (
            float(r.debt_209 or 0) > THR or float(r.debt_205 or 0) > THR
            or float(r.overpayment_209 or 0) > THR or float(r.overpayment_205 or 0) > THR
        )
        if not has_money:
            continue
        u = extra_users_map.get(r.user_id)
        rm = extra_rooms_map.get(r.room_id) if r.room_id else None
        extra_in_db.append({
            "user_id": r.user_id,
            "reading_id": r.id,
            "username": u.username if u else None,
            "room_label": (rm.format_address if rm else None),
            "actual": {
                "debt_209": float(r.debt_209 or 0),
                "overpayment_209": float(r.overpayment_209 or 0),
                "debt_205": float(r.debt_205 or 0),
                "overpayment_205": float(r.overpayment_205 or 0),
            },
        })

    drift.sort(key=lambda x: -x["max_abs_diff"])
    extra_in_db.sort(
        key=lambda x: -(
            x["actual"]["debt_209"] + x["actual"]["debt_205"]
            + x["actual"]["overpayment_209"] + x["actual"]["overpayment_205"]
        )
    )

    return {
        "period_id": period_id,
        "threshold_rub": THR,
        "latest_209_log_id": log_209.id if log_209 else None,
        "latest_205_log_id": log_205.id if log_205 else None,
        "summary": {
            "drift_count": len(drift),
            "missing_in_db_count": len(missing_in_db),
            "extra_in_db_count": len(extra_in_db),
            "expected_users": len(expected),
            "actual_readings": len(readings_by_user),
        },
        "drift": drift[:200],
        "missing_in_db": missing_in_db[:200],
        "extra_in_db": extra_in_db[:200],
    }


@router.post(
    "/debts/integrity-fix",
    summary="Авто-фикс расхождений integrity-check (Bug AK)",
)
async def debts_integrity_fix(
    category: str = Query("all", pattern="^(all|drift|missing|user)$"),
    user_id: Optional[int] = None,
    confirm: str = Query(..., pattern="^YES$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Применяет ожидаемые значения из applied_state свежих 209/205-импортов
    в БД. Покрывает категории:

      - **drift**: UPDATE существующих reading'ов до значений из applied_state
        (когда импорт правильно посчитал, но что-то после перезаписало).
      - **missing**: INSERT недостающих reading'ов из applied_state
        (когда жилец есть в файле, а в БД его reading нет).
      - **all**: drift + missing вместе.
      - **user**: фикс только для конкретного user_id (точечно).

    Extra/Zombie фиксится отдельным endpoint'ом /debts/cleanup-zombie-readings —
    у них нет «ожидаемого значения», только зануление.

    Требует ?confirm=YES.
    """
    _require_finance(current_user)

    # Реюзаем диагностику чтобы не дублировать логику расчёта расхождений.
    data = await debts_integrity_check(current_user=current_user, db=db)

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    if not active_period:
        raise HTTPException(404, "Нет активного периода")

    from sqlalchemy import update as _sa_update

    fixed_drift = 0
    fixed_missing = 0
    errors = []

    drift_items = data.get("drift", [])
    missing_items = data.get("missing_in_db", [])

    # Фильтр по user_id если category=user
    if category == "user":
        if not user_id:
            raise HTTPException(400, "category=user требует user_id")
        drift_items = [d for d in drift_items if d.get("user_id") == user_id]
        missing_items = [m for m in missing_items if m.get("user_id") == user_id]

    # 1) drift — UPDATE существующих reading'ов
    if category in ("all", "drift", "user"):
        for item in drift_items:
            try:
                res = await db.execute(
                    _sa_update(MeterReading)
                    .where(MeterReading.id == item["reading_id"])
                    .values(
                        debt_209=Decimal(str(item["expected"]["debt_209"])),
                        overpayment_209=Decimal(str(item["expected"]["overpayment_209"])),
                        debt_205=Decimal(str(item["expected"]["debt_205"])),
                        overpayment_205=Decimal(str(item["expected"]["overpayment_205"])),
                    )
                )
                if res.rowcount:
                    fixed_drift += 1
            except Exception as e:
                errors.append({
                    "kind": "drift",
                    "user_id": item.get("user_id"),
                    "error": str(e)[:200],
                })

    # 2) missing — INSERT недостающих reading'ов из applied_state
    if category in ("all", "missing", "user"):
        for item in missing_items:
            try:
                user = await db.get(User, item["user_id"])
                if not user:
                    errors.append({
                        "kind": "missing",
                        "user_id": item.get("user_id"),
                        "error": "user не найден в БД",
                    })
                    continue
                new_reading = MeterReading(
                    user_id=item["user_id"],
                    room_id=user.room_id,
                    period_id=active_period.id,
                    is_approved=False,
                    debt_209=Decimal(str(item["expected"]["debt_209"])),
                    overpayment_209=Decimal(str(item["expected"]["overpayment_209"])),
                    debt_205=Decimal(str(item["expected"]["debt_205"])),
                    overpayment_205=Decimal(str(item["expected"]["overpayment_205"])),
                    obor_debit_209=Decimal("0"), obor_credit_209=Decimal("0"),
                    obor_debit_205=Decimal("0"), obor_credit_205=Decimal("0"),
                )
                db.add(new_reading)
                fixed_missing += 1
            except Exception as e:
                errors.append({
                    "kind": "missing",
                    "user_id": item.get("user_id"),
                    "error": str(e)[:200],
                })

    await db.commit()
    logger.info(
        "[INTEGRITY-FIX] category=%s drift=%d missing=%d errors=%d (by %s)",
        category, fixed_drift, fixed_missing, len(errors), current_user.username,
    )

    return {
        "status": "ok",
        "category": category,
        "fixed_drift": fixed_drift,
        "fixed_missing": fixed_missing,
        "errors": errors[:50],
    }


@router.get(
    "/debts/zombie-readings",
    summary="Reading'и с долгом, которых нет в свежем импорте (Этап 3)",
)
async def debts_zombie_readings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bug AG cleanup: после переключения импорта на per-user-key (Bug AG)
    в БД могут остаться reading'и с ненулевыми debt_*/overpayment_*, которых
    в свежем impart'е 1С уже нет (т.е. в файле от 1С этого жильца не передавали,
    значит долг должен быть 0). Раньше эти суммы суммировались в общий
    reading комнаты — после Bug AG они становятся «висяком» на чужом юзере.

    Логика: смотрим последние completed-импорты 209 и 205, собираем все
    user_id из их applied_state. Все reading'и активного периода с
    долгом/переплатой, чей user_id НЕ упомянут ни в одном из этих логов —
    кандидаты на zombie.

    Read-only. POST /debts/cleanup-zombie-readings занулит их (с
    подтверждением).
    """
    _require_finance(current_user)

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    if not active_period:
        raise HTTPException(404, "Нет активного периода")
    period_id = active_period.id

    async def _latest_applied(acct: str):
        log = (await db.execute(
            select(DebtImportLog)
            .where(
                DebtImportLog.account_type == acct,
                DebtImportLog.status == "completed",
                DebtImportLog.applied_state.is_not(None),
            )
            .order_by(DebtImportLog.id.desc()).limit(1)
        )).scalars().first()
        return log

    log_209 = await _latest_applied("209")
    log_205 = await _latest_applied("205")
    state_209 = (log_209.applied_state or {}) if log_209 else {}
    state_205 = (log_205.applied_state or {}) if log_205 else {}
    known_user_ids = set(state_209.keys()) | set(state_205.keys())

    if not known_user_ids:
        return {
            "period_id": period_id,
            "count": 0,
            "zombies": [],
            "note": "Нет свежих импортов с applied_state — нечего сравнивать.",
        }

    readings = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.period_id == period_id,
            or_(
                MeterReading.debt_209 > 0,
                MeterReading.debt_205 > 0,
                MeterReading.overpayment_209 > 0,
                MeterReading.overpayment_205 > 0,
            ),
        )
    )).scalars().all()

    user_ids_in_db = {r.user_id for r in readings if r.user_id}
    if not user_ids_in_db:
        return {"period_id": period_id, "count": 0, "zombies": []}

    users = (await db.execute(
        select(User).where(User.id.in_(user_ids_in_db))
    )).scalars().all()
    users_map = {u.id: u for u in users}

    room_ids = {r.room_id for r in readings if r.room_id}
    rooms_map = {}
    if room_ids:
        rooms = (await db.execute(
            select(Room).where(Room.id.in_(room_ids))
        )).scalars().all()
        rooms_map = {r.id: r for r in rooms}

    zombies = []
    for r in readings:
        if not r.user_id:
            continue
        if str(r.user_id) in known_user_ids:
            continue  # есть в свежем импорте — не зомби
        user = users_map.get(r.user_id)
        room = rooms_map.get(r.room_id) if r.room_id else None
        zombies.append({
            "reading_id": r.id,
            "user_id": r.user_id,
            "username": user.username if user else None,
            "room_id": r.room_id,
            "room_label": (
                room.format_address if room else None
            ),
            "debt_209": float(r.debt_209 or 0),
            "overpayment_209": float(r.overpayment_209 or 0),
            "debt_205": float(r.debt_205 or 0),
            "overpayment_205": float(r.overpayment_205 or 0),
            "total_to_clean": (
                float(r.debt_209 or 0) + float(r.debt_205 or 0)
                + float(r.overpayment_209 or 0) + float(r.overpayment_205 or 0)
            ),
        })

    zombies.sort(key=lambda x: -x["total_to_clean"])
    return {
        "period_id": period_id,
        "latest_209_log_id": log_209.id if log_209 else None,
        "latest_205_log_id": log_205.id if log_205 else None,
        "count": len(zombies),
        "zombies": zombies,
    }


@router.post(
    "/debts/cleanup-zombie-readings",
    summary="Занулить debt_*/overpayment_* у zombie-reading'ов (Этап 3)",
)
async def debts_cleanup_zombie_readings(
    confirm: str = Query(..., pattern="^YES$"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Зануляет debt_209/205 и overpayment_209/205 у всех reading'ов,
    которые попали в список /debts/zombie-readings.

    Reading'и НЕ удаляются (audit/история сохраняется) — только зануляются
    финансовые поля. После этого дашборд показывает 0₽ у соответствующих
    жильцов.

    Требует ?confirm=YES.
    """
    _require_finance(current_user)

    # Реюзаем логику /debts/zombie-readings — чтобы не дублировать.
    result = await debts_zombie_readings(current_user=current_user, db=db)
    zombies = result.get("zombies", [])
    if not zombies:
        return {"status": "ok", "cleaned": 0, "note": "Zombie-reading'ов нет"}

    reading_ids = [z["reading_id"] for z in zombies]
    from sqlalchemy import update as _sa_update
    total = 0
    for rid in reading_ids:
        res = await db.execute(
            _sa_update(MeterReading)
            .where(MeterReading.id == rid)
            .values(
                debt_209=Decimal("0.00"),
                overpayment_209=Decimal("0.00"),
                debt_205=Decimal("0.00"),
                overpayment_205=Decimal("0.00"),
            )
        )
        total += res.rowcount or 0

    await db.commit()
    logger.info(
        "[ZOMBIE-CLEANUP] %s reading-ов занулено (запросил %s)",
        total, current_user.username,
    )
    return {
        "status": "ok",
        "cleaned": total,
        "requested": len(reading_ids),
        "zombies": zombies[:50],
    }


@router.get(
    "/debts/orphan-readings",
    summary="Жильцы с >1 reading в активном периоде (диагностика для Bug AF)",
)
async def debts_orphan_readings(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bug AF: после переездов / auto-Vacant может оказаться, что у одного
    user_id в активном периоде существует несколько MeterReading с разными
    room_id. Дашборд агрегирует SUM(debt_*) по user_id, а импорт 1С
    обновляет reading по room_id. Если осиротевший reading с прошлой
    комнатой не зачищен — его debt_209 продолжает суммироваться в общий,
    и reparse его не чинит (импорт обновляет только текущий room_id).

    Read-only endpoint: возвращает список жильцов с >1 reading + раскладку
    каждого reading'а (room_id, debt, current/orphan). По нему уже решаем,
    что чистить — отдельный POST-endpoint.
    """
    _require_finance(current_user)

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    if not active_period:
        raise HTTPException(404, "Нет активного периода")
    period_id = active_period.id

    dup_q = (
        select(MeterReading.user_id, func.count(MeterReading.id).label("cnt"))
        .where(
            MeterReading.period_id == period_id,
            MeterReading.user_id.is_not(None),
        )
        .group_by(MeterReading.user_id)
        .having(func.count(MeterReading.id) > 1)
    )
    dup_rows = (await db.execute(dup_q)).all()
    user_ids = [r.user_id for r in dup_rows]
    if not user_ids:
        return {"period_id": period_id, "count": 0, "users": []}

    readings = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.period_id == period_id,
            MeterReading.user_id.in_(user_ids),
        )
        .order_by(MeterReading.user_id, MeterReading.created_at)
    )).scalars().all()

    users = (await db.execute(
        select(User).where(User.id.in_(user_ids))
    )).scalars().all()
    users_map = {u.id: u for u in users}

    room_ids = {r.room_id for r in readings if r.room_id}
    room_ids.update(u.room_id for u in users if u.room_id)
    rooms_map = {}
    if room_ids:
        rooms = (await db.execute(
            select(Room).where(Room.id.in_(room_ids))
        )).scalars().all()
        rooms_map = {r.id: r for r in rooms}

    def _room_label(rid):
        if rid is None:
            return None
        r = rooms_map.get(rid)
        return r.format_address if r else f"id={rid}"

    by_user: dict[int, list] = {}
    for r in readings:
        by_user.setdefault(r.user_id, []).append(r)

    items = []
    for uid, urs in by_user.items():
        user = users_map.get(uid)
        cur_room_id = user.room_id if user else None
        orphan_debt = sum(
            float(r.debt_209 or 0) + float(r.debt_205 or 0)
            for r in urs if r.room_id != cur_room_id
        )
        items.append({
            "user_id": uid,
            "username": user.username if user else None,
            "current_room_id": cur_room_id,
            "current_room_label": _room_label(cur_room_id),
            "total_debt_209": sum(float(r.debt_209 or 0) for r in urs),
            "total_debt_205": sum(float(r.debt_205 or 0) for r in urs),
            "orphan_debt_sum": orphan_debt,
            "readings": [
                {
                    "id": r.id,
                    "room_id": r.room_id,
                    "room_label": _room_label(r.room_id) or "(нет комнаты)",
                    "is_current_room": (r.room_id == cur_room_id),
                    "debt_209": float(r.debt_209 or 0),
                    "overpayment_209": float(r.overpayment_209 or 0),
                    "debt_205": float(r.debt_205 or 0),
                    "overpayment_205": float(r.overpayment_205 or 0),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "is_approved": r.is_approved,
                }
                for r in urs
            ],
        })

    # Сортировка: сначала те, у кого больше всего «осиротевших» денег.
    items.sort(key=lambda x: -x["orphan_debt_sum"])

    return {
        "period_id": period_id,
        "count": len(items),
        "users": items,
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
                u.room.format_address if u.room else "без комнаты"
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
            u.room.format_address if u.room else "без комнаты"
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
