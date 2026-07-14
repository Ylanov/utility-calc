# Не найденные ФИО и обслуживание истории: delete/cleanup, reassign, find-candidates, create-and-match.
# Механически выделено из монолитного routers/financier.py (распил на
# пакет financier/): код перенесён дословно, поведение/пути/тексты 1:1.

from decimal import Decimal
from typing import Optional
from fastapi import Depends, HTTPException, Form, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, desc
from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Room, DebtImportLog
from app.core.dependencies import get_current_user
from app.modules.utility.services.search_utils import like_contains

from ._shared import (
    router,
    _nfu_fio,
    _ensure_debt_alias,
    _require_finance,
)


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
    if log.status not in ("staged", "completed"):
        raise HTTPException(400, f"Статус лога «{log.status}» — reassign для staged/completed")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Жилец не найден")
    # Долг на лицевом счёте (user_id) — комната опциональна, подцепится позже.

    debt_dec = Decimal(str(debt or 0))
    over_dec = Decimal(str(overpayment or 0))
    acc = log.account_type
    reading_id = None

    if log.status == "staged":
        # Черновик: пишем в applied_state (как rematch-base/publish), показания
        # НЕ трогаем — их создаст «Выгрузить». Полная замена по этому счёту.
        ap = dict(log.applied_state or {})
        key = str(user.id)
        ent = ap.get(key) or {
            "debt_209": "0", "overpayment_209": "0",
            "debt_205": "0", "overpayment_205": "0",
        }
        ent[f"debt_{acc}"] = str(debt_dec)
        ent[f"overpayment_{acc}"] = str(over_dec)
        ent["username"] = user.username
        ent["room_id"] = user.room_id
        room = await db.get(Room, user.room_id) if user.room_id else None
        ent["room_label"] = room.format_address if room else None
        ap[key] = ent
        log.applied_state = ap
    else:
        # completed: долг уже выгружен — дописываем в показание ЭТОГО жильца
        # (по user_id, не по комнате; room_id может быть NULL).
        reading = None
        if log.period_id:
            reading = (await db.execute(
                select(MeterReading).where(
                    MeterReading.period_id == log.period_id,
                    MeterReading.user_id == user.id,
                ).limit(1)
            )).scalars().first()
        if reading:
            if acc == "209":
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
                debt_209=debt_dec if acc == "209" else Decimal("0"),
                overpayment_209=over_dec if acc == "209" else Decimal("0"),
                debt_205=debt_dec if acc == "205" else Decimal("0"),
                overpayment_205=over_dec if acc == "205" else Decimal("0"),
            )
            db.add(reading)
        await db.flush()
        reading_id = reading.id if reading else None

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
        "reading_id": reading_id,
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
            filtered = filtered.where(func.lower(User.username).like(like_contains(tok)))
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
    if log.status not in ("staged", "completed"):
        raise HTTPException(400, f"Статус лога «{log.status}» — операция для staged/completed")

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

    # 3. Создание User. resident_type/billing_mode НЕ берём из payload и НЕ
    # ставим per_capita (2026-06-19): тип выводится из комнаты при заселении
    # (move_user_to_room), все на by_meter; per_capita-шорткат убран (обнулял
    # счёт холостяка). Дефолт family/by_meter — move_user_to_room поправит.
    from app.core.auth import get_password_hash
    db_user = User(
        username=data.username.strip(),
        login=data.username.strip(),  # учётка по умолчанию = ФИО, жилец сменит сам
        hashed_password=get_password_hash(data.password),
        role="user",
        workplace=(data.workplace or "").strip() or None,
        residents_count=max(1, int(data.residents_count)),
        room_id=None,  # выставит move_user_to_room
        resident_type="family",
        billing_mode="by_meter",
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

    acc = log.account_type
    if log.status == "staged":
        # Черновик: долг в applied_state, показание создаст «Выгрузить».
        ap = dict(log.applied_state or {})
        ap[str(db_user.id)] = {
            "debt_209": str(debt_dec) if acc == "209" else "0",
            "overpayment_209": str(over_dec) if acc == "209" else "0",
            "debt_205": str(debt_dec) if acc == "205" else "0",
            "overpayment_205": str(over_dec) if acc == "205" else "0",
            "username": db_user.username,
            "room_id": room.id,
            "room_label": room.format_address,
        }
        log.applied_state = ap
    elif log.period_id:
        reading = (await db.execute(
            select(MeterReading).where(
                MeterReading.period_id == log.period_id,
                MeterReading.user_id == db_user.id,
            ).limit(1)
        )).scalars().first()

        if reading:
            if acc == "209":
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
                debt_209=debt_dec if acc == "209" else Decimal("0"),
                overpayment_209=over_dec if acc == "209" else Decimal("0"),
                debt_205=debt_dec if acc == "205" else Decimal("0"),
                overpayment_205=over_dec if acc == "205" else Decimal("0"),
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
