# Импорт долгов из Excel 1С: превью файла, фоновый импорт, парная загрузка 205+209.
# Механически выделено из монолитного routers/financier.py (распил на
# пакет financier/): код перенесён дословно, поведение/пути/тексты 1:1.

import uuid
from fastapi import Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from app.core.database import get_db
from app.modules.utility.models import User, DebtImportLog
from app.core.dependencies import get_current_user
from app.modules.utility.tasks import import_debts_task

from ._shared import (
    router,
    logger,
    MAX_FILE_SIZE,
    _save_uploaded_debt_file,
)


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
        period_id: int | None = Form(None, description="Период загрузки; по умолчанию активный"),
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
        period_id=period_id,
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
        period_id: int | None = Form(None, description="Период загрузки; по умолчанию активный"),
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
            period_id=period_id,
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
