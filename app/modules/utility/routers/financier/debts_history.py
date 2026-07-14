# История импортов 1С: список, diff, история жильца, download, reparse, not-found, undo.
# Механически выделено из монолитного routers/financier.py (распил на
# пакет financier/): код перенесён дословно, поведение/пути/тексты 1:1.

import os
from decimal import Decimal
from app.core.time_utils import utcnow
from typing import Optional
from fastapi import Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, DebtImportLog
from app.core.dependencies import get_current_user
from app.modules.utility.tasks import import_debts_task

from ._shared import (
    router,
    logger,
    _require_finance,
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


@router.get("/debts/import-history/{log_id}/not-found-analysis",
            summary="Почему ФИО не сматчились: категории + лучший кандидат")
async def debts_not_found_analysis(
    log_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Диагностика: для КАЖДОГО ненайденного ФИО считает лучшего кандидата в
    базе жильцов (fuzzy + совпадение фамилии) и относит к категории:
      • near    (score ≥ 70) — близкое совпадение ЕСТЬ: ФИО просто записано в 1С
        иначе (сокращение/формат/опечатка). Привязать в 1 клик (reassign).
      • weak    (50–69)      — совпала фамилия, но имя/отчество расходятся
        (возможен однофамилец) — нужна проверка.
      • absent  (< 50)       — похожих в базе нет: бывший жилец, новый человек,
        или не-резидент-плательщик.
    Так видно, сколько из N «не найдено» — это формат (быстрый reassign), а
    сколько реально отсутствуют в базе."""
    _require_finance(current_user)
    log = await db.get(DebtImportLog, log_id)
    if not log:
        raise HTTPException(404, "Лог импорта не найден")

    raw = log.not_found_users or []
    fios = []
    for item in raw:
        if isinstance(item, dict):
            fios.append((item.get("fio", ""), item.get("debt", "0"), item.get("overpayment", "0")))
        else:
            fios.append((str(item), "0", "0"))

    from sqlalchemy.orm import selectinload as _selectinload
    from rapidfuzz import fuzz
    users = (await db.execute(
        select(User).options(_selectinload(User.room))
        .where(User.is_deleted.is_(False), User.role == "user")
    )).scalars().all()
    user_norm = [(u, " ".join((u.username or "").lower().split())) for u in users if u.username]

    def _parts(s: str):
        # ФИО → [фамилия, имя, отчество]: lower, ё→е, точки как разделители.
        t = (s or "").lower().replace("ё", "е").replace(".", " ")
        return t.split()

    def _pm(a: str, b: str) -> bool:
        # Совпадение части ФИО: равны / инициал (одна буква совпала) / опечатка.
        if not a or not b:
            return False
        if a == b:
            return True
        if len(a) == 1 or len(b) == 1:
            return a[0] == b[0]
        return fuzz.ratio(a, b) >= 88

    # Пер-categories. ВАЖНО: «тот же человек» = совпали ВСЕ три части (фамилия+
    # имя+отчество), а не только фамилия. Иначе «Верхозин Владимир» матчился бы
    # на «Верхозин Артём» (однофамилец) с виду «привязать в 1 клик» — и долг ушёл
    # бы чужому. Однофамилец/совпавшее имя-отчество = РАЗНЫЙ человек, не привязка.
    cats = {"same": 0, "namesake": 0, "absent": 0}
    items = []
    for fio, debt, overpay in fios:
        fp = _parts(fio)
        best_u = None
        best_key = (-1, -1)   # (совпавших_частей, fuzzy)
        best_flags = (False, False, False)
        best_fuzzy = 0
        for u, _nn in user_norm:
            cp = _parts(u.username)
            if not fp or not cp:
                continue
            s = _pm(fp[0], cp[0])
            n = _pm(fp[1] if len(fp) > 1 else "", cp[1] if len(cp) > 1 else "")
            p = _pm(fp[2] if len(fp) > 2 else "", cp[2] if len(cp) > 2 else "")
            fz = fuzz.token_sort_ratio(" ".join(fp), " ".join(cp))
            key = (int(s) + int(n) + int(p), fz)
            if key > best_key:
                best_key, best_u, best_flags, best_fuzzy = key, u, (s, n, p), fz
        s, n, p = best_flags
        if best_u is None:
            cat, reason = "absent", None
        elif s and n and p:
            cat, reason = "same", "фамилия+имя+отчество совпали"
        elif s:
            cat, reason = "namesake", "та же фамилия, имя/отчество другие"
        elif n and p:
            cat, reason = "namesake", "совпали имя+отчество, фамилия другая"
        elif best_fuzzy >= 60:
            cat, reason = "namesake", "частичное совпадение"
        else:
            cat, reason = "absent", None
        cats[cat] += 1
        cand = None
        if best_u is not None and cat != "absent":
            cand = {
                "id": best_u.id,
                "username": best_u.username,
                "room": (best_u.room.format_address if best_u.room else None),
            }
        items.append({
            "fio": fio,
            "debt": debt,
            "overpayment": overpay,
            "best_score": int(best_fuzzy),
            "category": cat,
            "reason": reason,
            "candidate": cand,
        })

    items.sort(key=lambda x: -x["best_score"])
    return {
        "log_id": log.id,
        "account_type": log.account_type,
        "total": len(items),
        "categories": cats,
        "items": items,
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

    before = log.snapshot_data.get("before") or {}
    inserted_ids = log.snapshot_data.get("inserted_reading_ids") or []
    if not isinstance(before, dict):
        before = {}
    if not isinstance(inserted_ids, list):
        inserted_ids = []
    inserted_ids = [i for i in inserted_ids if isinstance(i, int)]

    # 1. Восстанавливаем существующие readings из snapshot.
    # ЗАЩИТА: снимок может быть пустым/иного формата (старые или ГИС-импорты) —
    # пропускаем некорректные записи, не роняем 500.
    def _dec(v):
        try:
            return Decimal(str(v if v not in (None, "") else "0"))
        except Exception:
            return Decimal("0")
    updates = []
    for reading_id_str, vals in before.items():
        if not isinstance(vals, dict):
            continue
        try:
            rid = int(reading_id_str)
        except (TypeError, ValueError):
            continue
        updates.append({
            "id": rid,
            "debt_209": _dec(vals.get("debt_209")),
            "overpayment_209": _dec(vals.get("overpayment_209")),
            "debt_205": _dec(vals.get("debt_205")),
            "overpayment_205": _dec(vals.get("overpayment_205")),
        })
    if not updates and not inserted_ids:
        raise HTTPException(
            400, "Снимок импорта пуст или несовместим — откат не применён "
                 "(вероятно, ГИС-импорт или старый формат). Долги не изменены.")

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
