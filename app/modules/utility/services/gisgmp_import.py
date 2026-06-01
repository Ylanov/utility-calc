# app/modules/utility/services/gisgmp_import.py
"""
Приём находок ГИС ГМП от релея — РАЗДЕЛЬНЫЙ режим (отладка).

ВАЖНО (пока): данные ГИС ГМП НЕ пишутся в долги показаний (MeterReading) и НЕ
смешиваются с ручным импортом Excel. Релей присылает распарсенные начисления,
мы:
  • отбрасываем аннулированные, берём «Не сквитировано» (= долг);
  • разносим по счетам: «наем» → 205, «комуслуги» → 209;
  • суммируем по ФИО плательщика;
  • сопоставляем ФИО с жильцом в базе (как Google-Sheets-импорт) — ТОЛЬКО для
    показа (кого нашли/не нашли);
  • складываем всё в отдельное хранилище (SystemSetting 'gisgmp_findings') —
    его показывает отдельное окно во вкладке «Долги 1С» для отладки.

Когда отладим и решим — подключим запись в долги. Сейчас задача: видеть, что
именно находит система, отдельно от Excel.
"""
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.modules.utility.models import SystemSetting
from app.modules.utility.services.debt_import import clean_decimal
from app.modules.utility.services.gsheets_sync import (
    build_users_index, build_aliases_index, match_user,
)

logger = logging.getLogger(__name__)

# Метка источника (для совместимости со старым кодом, если где-то ссылается).
GISGMP_SOURCE_LABEL = "ГИС ГМП (авто)"
# Ключ хранилища находок (отдельно от долгов).
GISGMP_FINDINGS_KEY = "gisgmp_findings"
# Сколько сырых начислений хранить для показа (защита от разрастания).
_RAW_CAP = 3000


def classify_account(purpose: str) -> Optional[str]:
    """«наем/найм» → 205 (найм), «комуслуги/коммунальные» → 209 (коммуналка)."""
    p = (purpose or "").lower()
    if "наем" in p or "найм" in p or "наём" in p:
        return "205"
    if "комус" in p or "коммунал" in p:
        return "209"
    return None


def is_unpaid(ack_status: str) -> bool:
    """Долг = начисление со статусом «Не сквитировано» (остальные «...сквитировано» = оплачено)."""
    return "не сквитировано" in (ack_status or "").lower()


def is_annulled(change_status: str) -> bool:
    """«аннулирование» — отменённое (не «деаннулирование»), в долг не идёт."""
    return (change_status or "").strip().lower() == "аннулирование"


def aggregate_charges(charges: list[dict]) -> tuple[dict, dict]:
    """Сворачивает начисления в {fio: {"209": Decimal, "205": Decimal}} + диагностика."""
    fio_map: dict[str, dict[str, Decimal]] = {}
    diag = {"total": 0, "annulled": 0, "paid": 0, "unknown_account": 0, "no_fio": 0, "counted": 0}
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
    """Раздельный режим: сворачивает находки, сопоставляет жильцов (для показа),
    складывает в SystemSetting('gisgmp_findings'). В долги/показания НЕ пишет."""
    fio_map, diag = aggregate_charges(charges)

    # Индексы жильцов + алиасы — тот же матчер, что Google-Sheets-импорт.
    users_map, users_keys, users_by_id = build_users_index(db)
    aliases_map = build_aliases_index(db)

    summary = []
    matched = 0
    for fio, debts in fio_map.items():
        info, score, _conflict = match_user(
            fio, None, users_map, users_keys, users_by_id, aliases_map,
        )
        d209 = debts.get("209", Decimal("0"))
        d205 = debts.get("205", Decimal("0"))
        if info:
            matched += 1
        summary.append({
            "fio": fio,
            "debt_209": str(d209),
            "debt_205": str(d205),
            "total": str(d209 + d205),
            "matched_user_id": info["id"] if info else None,
            "matched_username": info.get("username") if info else None,
            "room_number": info.get("room_number") if info else None,
            "score": int(score),
        })
    summary.sort(key=lambda r: -float(r["total"]))

    findings = {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "total_charges": len(charges),
        "residents": len(fio_map),
        "matched": matched,
        "not_found": len(fio_map) - matched,
        "diag": diag,
        "summary": summary,
        "charges": charges[:_RAW_CAP],
    }

    row = db.query(SystemSetting).filter(SystemSetting.key == GISGMP_FINDINGS_KEY).first()
    if row is None:
        row = SystemSetting(key=GISGMP_FINDINGS_KEY, value="{}",
                            description="Находки релея ГИС ГМП (отладка, отдельно от долгов)")
        db.add(row)
    row.value = json.dumps(findings, ensure_ascii=False)
    db.commit()

    result = {
        "status": "ok",
        "total_charges": len(charges),
        "residents": len(fio_map),
        "matched": matched,
        "not_found": len(fio_map) - matched,
        "diag": diag,
    }
    logger.info("[GISGMP] findings stored: %s", result)
    return result
