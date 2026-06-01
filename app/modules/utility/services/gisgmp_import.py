# app/modules/utility/services/gisgmp_import.py
"""
Авто-подгрузка долгов из реестра ГИС ГМП (gisgmp.cgu.mchs.ru).

Браузерное расширение `gisgmp-bridge` под ЭЦП-сессией пользователя раз в ~12 ч
обходит раздел «Начисления», парсит строки и POST'ит сюда массив начислений
(см. financier.gisgmp_sync). Здесь мы:
  • отбрасываем аннулированные начисления;
  • берём только «Не сквитировано» (непогашенный остаток = долг);
  • разносим по счетам 1С по «Назначению»: «наем» → 205, «комуслуги» → 209;
  • суммируем по ФИО плательщика;
  • матчим жильца тем же матчером, что и Google-Sheets-импорт
    (точное ФИО → инициалы → fuzzy → алиасы);
  • пишем debt_209/debt_205 в MeterReading активного периода;
  • создаём пару DebtImportLog (209 + 205) — те же история/diff/откат, что у Excel.

Договорённость «только долги»: overpayment_* и обороты для затронутых жильцов
обнуляются — реестр пока не отдаёт переплат. Прежние значения сохраняются в
snapshot_data, поэтому откат импорта (undo) их восстановит.

ГИС ГМП — источник истины по долгу для тех ФИО, что в неё попали. Жильцы,
которых в выгрузке нет, остаются с прежним сальдо (как и при Excel-импорте;
«зомби»-долги ловит отдельная проверка во вкладке «Долги 1С»).
"""
import logging
import uuid as _uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, update as sa_update
from sqlalchemy.orm import Session

from app.modules.utility.models import (
    MeterReading, BillingPeriod, DebtImportLog, Room,
)
from app.modules.utility.services.debt_import import clean_decimal
from app.modules.utility.services.gsheets_sync import (
    build_users_index, build_aliases_index, match_user,
)

logger = logging.getLogger(__name__)

# Источник в file_name/started_by_username — по нему GET /gisgmp/status находит
# последний синк и UI отличает авто-подгрузку от ручного Excel-импорта.
GISGMP_SOURCE_LABEL = "ГИС ГМП (авто)"


def classify_account(purpose: str) -> Optional[str]:
    """«наем/найм» → 205 (найм), «комуслуги/коммунальные» → 209 (коммуналка).

    None — назначение не распознано, такое начисление не разносим (попадёт
    в диагностику unknown_account, деньги не теряются — видно в отчёте синка).
    """
    p = (purpose or "").lower()
    if "наем" in p or "найм" in p or "наём" in p:
        return "205"
    if "комус" in p or "коммунал" in p:
        return "209"
    return None


def is_unpaid(ack_status: str) -> bool:
    """Долг = начисление со статусом квитирования «Не сквитировано».

    Все варианты «...сквитировано» (Предварительно/Принудительно/с зачислением)
    означают, что платёж найден → начисление погашено, в долг не идёт.
    Подстрока «не сквитировано» не встречается ни в одном из «оплачено»-статусов.
    """
    return "не сквитировано" in (ack_status or "").lower()


def is_annulled(change_status: str) -> bool:
    """«аннулирование» — отменённое начисление (исключаем из долга).

    ВАЖНО: «деаннулирование» означает обратное (начисление снова действует),
    поэтому сверяем строго по равенству, а не по подстроке «аннул».
    """
    return (change_status or "").strip().lower() == "аннулирование"


def aggregate_charges(charges: list[dict]) -> tuple[dict, dict]:
    """Сворачивает начисления в {fio: {"209": Decimal, "205": Decimal}}.

    Возвращает (fio_map, diag), где diag — счётчики для отчёта синка.
    """
    fio_map: dict[str, dict[str, Decimal]] = {}
    diag = {
        "total": 0, "annulled": 0, "paid": 0,
        "unknown_account": 0, "no_fio": 0, "counted": 0,
    }
    for ch in charges:
        diag["total"] += 1
        fio = (ch.get("payer_name") or "").strip()
        if not fio:
            diag["no_fio"] += 1
            continue
        if is_annulled(ch.get("change_status")):
            diag["annulled"] += 1
            continue
        if not is_unpaid(ch.get("ack_status")):
            diag["paid"] += 1
            continue
        account = classify_account(ch.get("purpose"))
        if account is None:
            diag["unknown_account"] += 1
            continue
        amount = clean_decimal(ch.get("amount"))
        if amount <= 0:
            continue
        slot = fio_map.setdefault(fio, {"209": Decimal("0"), "205": Decimal("0")})
        slot[account] += amount
        diag["counted"] += 1
    return fio_map, diag


def sync_import_gisgmp_charges(
    charges: list[dict],
    db: Session,
    *,
    started_by_username: str = GISGMP_SOURCE_LABEL,
    started_by_id: Optional[int] = None,
) -> dict:
    """Главный вход: пишет долги ГИС ГМП в активный период.

    Создаёт ПАРУ DebtImportLog (209 + 205) под одним batch_id — так вкладка
    «Долги 1С» (история, diff, откат, «не найдены») работает без изменений.
    Возвращает сводку для расширения и UI.
    """
    fio_map, diag = aggregate_charges(charges)

    active_period = db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    ).scalars().first()
    if not active_period:
        return {
            "status": "error",
            "message": "Нет активного периода для загрузки долгов",
            "diag": diag,
        }

    # Индексы жильцов + алиасы — тот же матчер, что и Google-Sheets-импорт.
    users_map, users_keys, users_by_id = build_users_index(db)
    aliases_map = build_aliases_index(db)

    # Показания активного периода: ключ — user_id (как в долговом импорте).
    readings_raw = db.execute(
        select(MeterReading).where(MeterReading.period_id == active_period.id)
    ).scalars().all()
    readings_map = {r.user_id: r for r in readings_raw if r.user_id is not None}

    snapshot_before: dict[int, dict] = {}          # reading_id -> до-состояние (для undo)
    not_found = {"209": [], "205": []}             # ФИО без привязки, по счетам
    touched_meta: list[dict] = []                  # затронутые existing (до expunge)
    inserts: list[MeterReading] = []
    insert_meta: list[dict] = []
    stats = {"matched": 0, "created": 0, "updated": 0}

    for fio, debts in fio_map.items():
        debt_209 = debts.get("209", Decimal("0"))
        debt_205 = debts.get("205", Decimal("0"))

        user_info, _score, _conflict = match_user(
            fio, None, users_map, users_keys, users_by_id, aliases_map,
        )
        if not user_info or not user_info.get("room_id"):
            # Не привязали (нет жильца / нет комнаты) — долг учитываем
            # отдельно в «Неразнесённых», деньги не теряются.
            if debt_209 > 0:
                not_found["209"].append(
                    {"fio": fio, "debt": str(debt_209), "overpayment": "0"})
            if debt_205 > 0:
                not_found["205"].append(
                    {"fio": fio, "debt": str(debt_205), "overpayment": "0"})
            continue

        user_id = user_info["id"]
        room_id = user_info["room_id"]
        stats["matched"] += 1

        reading = readings_map.get(user_id)
        if reading is not None:
            if reading.id not in snapshot_before:
                snapshot_before[reading.id] = {
                    "debt_209": str(reading.debt_209 or 0),
                    "overpayment_209": str(reading.overpayment_209 or 0),
                    "debt_205": str(reading.debt_205 or 0),
                    "overpayment_205": str(reading.overpayment_205 or 0),
                }
            # ГИС ГМП — источник истины: ставим долг, обнуляем переплату/обороты.
            reading.debt_209 = debt_209
            reading.overpayment_209 = Decimal("0.00")
            reading.obor_debit_209 = Decimal("0.00")
            reading.obor_credit_209 = Decimal("0.00")
            reading.debt_205 = debt_205
            reading.overpayment_205 = Decimal("0.00")
            reading.obor_debit_205 = Decimal("0.00")
            reading.obor_credit_205 = Decimal("0.00")
            touched_meta.append({
                "id": reading.id, "user_id": user_id, "room_id": room_id,
                "debt_209": debt_209, "debt_205": debt_205,
            })
            stats["updated"] += 1
        else:
            reading = MeterReading(
                user_id=user_id, room_id=room_id, period_id=active_period.id,
                is_approved=False,
                debt_209=debt_209, overpayment_209=Decimal("0.00"),
                debt_205=debt_205, overpayment_205=Decimal("0.00"),
                obor_debit_209=Decimal("0.00"), obor_credit_209=Decimal("0.00"),
                obor_debit_205=Decimal("0.00"), obor_credit_205=Decimal("0.00"),
            )
            inserts.append(reading)
            insert_meta.append({
                "user_id": user_id, "room_id": room_id,
                "debt_209": debt_209, "debt_205": debt_205,
            })
            readings_map[user_id] = reading
            stats["created"] += 1

    # ─── Запись в БД ────────────────────────────────────────────────────────
    inserted_ids: list[int] = []
    if inserts:
        db.add_all(inserts)
        db.flush()
        inserted_ids = [r.id for r in inserts]
        for meta, r in zip(insert_meta, inserts):
            meta["id"] = r.id

    if touched_meta:
        # MeterReading партиционирована — bulk_update тихо не пишет; используем
        # explicit per-row UPDATE по id (см. Bug в debt_import: Лучка А.П.).
        # Отвязываем объекты от сессии, чтобы ORM-flush не перезаписал UPDATE.
        for m in touched_meta:
            obj = db.get(MeterReading, m["id"])
            if obj is not None:
                try:
                    db.expunge(obj)
                except Exception:
                    pass
        for m in touched_meta:
            db.execute(
                sa_update(MeterReading)
                .where(MeterReading.id == m["id"])
                .values(
                    debt_209=m["debt_209"], overpayment_209=Decimal("0.00"),
                    obor_debit_209=Decimal("0.00"), obor_credit_209=Decimal("0.00"),
                    debt_205=m["debt_205"], overpayment_205=Decimal("0.00"),
                    obor_debit_205=Decimal("0.00"), obor_credit_205=Decimal("0.00"),
                )
            )

    # ─── applied_state (state ПОСЛЕ, denormalized для diff/истории) ─────────
    all_meta = touched_meta + insert_meta
    room_ids = list({m["room_id"] for m in all_meta if m.get("room_id")})
    rooms_label: dict[int, str] = {}
    if room_ids:
        for room in db.execute(select(Room).where(Room.id.in_(room_ids))).scalars().all():
            rooms_label[room.id] = room.format_address

    applied_state: dict[str, dict] = {}
    for m in all_meta:
        uid = m["user_id"]
        info = users_by_id.get(uid) or {}
        applied_state[str(uid)] = {
            "debt_209": str(m.get("debt_209") or 0),
            "overpayment_209": "0",
            "debt_205": str(m.get("debt_205") or 0),
            "overpayment_205": "0",
            "username": info.get("username"),
            "room_id": m.get("room_id"),
            "room_label": rooms_label.get(m.get("room_id")),
        }

    # ─── Пара DebtImportLog (209 + 205) под одним batch_id ──────────────────
    batch_id = str(_uuid.uuid4())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for account in ("209", "205"):
        log = DebtImportLog(
            account_type=account,
            period_id=active_period.id,
            file_name=GISGMP_SOURCE_LABEL,
            status="completed",
            started_by_id=started_by_id,
            started_by_username=started_by_username,
            processed=len(fio_map),
            updated=stats["updated"],
            created=stats["created"],
            not_found_count=len(not_found[account]),
            not_found_users=not_found[account][:2000],
            snapshot_data={
                "before": {str(k): v for k, v in snapshot_before.items()},
                "inserted_reading_ids": inserted_ids,
            },
            applied_state=applied_state,
            batch_id=batch_id,
            completed_at=now,
        )
        db.add(log)

    db.commit()

    result = {
        "status": "ok",
        "period_id": active_period.id,
        "period_name": active_period.name,
        "batch_id": batch_id,
        "matched": stats["matched"],
        "updated": stats["updated"],
        "created": stats["created"],
        "not_found_209": len(not_found["209"]),
        "not_found_205": len(not_found["205"]),
        "diag": diag,
    }
    logger.info("[GISGMP] sync done: %s", result)
    return result
