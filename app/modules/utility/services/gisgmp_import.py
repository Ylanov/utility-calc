# app/modules/utility/services/gisgmp_import.py
"""
Приём находок ГИС ГМП от релея — ИНКРЕМЕНТАЛЬНЫЙ КЭШ по УИН (отладочный/раздельный режим).

ГИС-сервер медленный, а долг накопительный (нужна вся история, не окно). Поэтому:
  • ЖКХ копит ВСЕ начисления по УИН в кэше (SystemSetting 'gisgmp_cache') —
    релей при каждом прогоне досылает только новое/изменённое (по дате актуализации);
  • из полного кэша пересчитываем долг по жильцам (наем→205, комуслуги→209,
    «Не сквитировано»=долг, «аннулирование»→мимо), матчим ФИО→жилец;
  • результат кладём в 'gisgmp_findings' (его читают UI «Показать найденное»,
    поиск по фамилии и «Сверка с 1С»).

ВАЖНО: в долги показаний (MeterReading/Excel) НЕ пишем — пока раздельно, для
отладки. Курсор инкремента (макс дата актуализации) — в 'gisgmp_cursor',
релей берёт его из relay-config как `since` и не перечитывает старое.
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

GISGMP_SOURCE_LABEL = "ГИС ГМП (авто)"
GISGMP_FINDINGS_KEY = "gisgmp_findings"
GISGMP_CACHE_KEY = "gisgmp_cache"      # {uin: charge_dict} — накопительный кэш
GISGMP_CURSOR_KEY = "gisgmp_cursor"    # {"since": ISO} — макс дата актуализации
_CACHE_CAP = 20000                     # потолок кэша (защита от разрастания)
_FINDINGS_CHARGES_CAP = 8000           # сколько сырых строк отдать в UI-поиск

# Поля одного начисления, которые храним в кэше (как присылает релей).
_CHARGE_FIELDS = (
    "uin", "amount", "bill_date", "actualize_date", "account",
    "payer_name", "purpose", "ack_status", "change_status", "source", "charge_uuid",
)


def classify_account(purpose: str) -> Optional[str]:
    p = (purpose or "").lower()
    if "наем" in p or "найм" in p or "наём" in p:
        return "205"
    if "комус" in p or "коммунал" in p:
        return "209"
    return None


def is_unpaid(ack_status: str) -> bool:
    return "не сквитировано" in (ack_status or "").lower()


def is_annulled(change_status: str) -> bool:
    return (change_status or "").strip().lower() == "аннулирование"


def parse_reg_dt(s: str) -> Optional[datetime]:
    """Дата реестра «ДД.ММ.ГГГГ ЧЧ:ММ» (или без времени) → datetime."""
    s = (s or "").strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def aggregate_charges(charges) -> tuple[dict, dict]:
    """{fio: {"209": Decimal, "205": Decimal}} по непогашенным + диагностика."""
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


# ─── helpers для SystemSetting (sync-сессия) ─────────────────────────────────

def _read_json(db: Session, key: str, default):
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row and row.value:
        try:
            return json.loads(row.value)
        except Exception:
            return default
    return default


def _write_json(db: Session, key: str, value, desc: str = ""):
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is None:
        row = SystemSetting(key=key, value="{}", description=desc)
        db.add(row)
    row.value = json.dumps(value, ensure_ascii=False)


def _recompute_findings(db: Session, cache: dict) -> dict:
    """Пересчёт находок из ПОЛНОГО кэша: сводка по жильцам + сырые строки."""
    charges = list(cache.values())
    fio_map, diag = aggregate_charges(charges)

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
            "debt_209": str(d209), "debt_205": str(d205), "total": str(d209 + d205),
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
        "charges": charges[:_FINDINGS_CHARGES_CAP],
    }
    _write_json(db, GISGMP_FINDINGS_KEY, findings, "Находки ГИС ГМП (пересчёт из кэша)")
    return findings


def sync_import_gisgmp_charges(
    charges: list[dict],
    db: Session,
    *,
    started_by_username: str = GISGMP_SOURCE_LABEL,
    started_by_id: Optional[int] = None,
) -> dict:
    """Инкрементальный приём: доливаем новое/изменённое в кэш по УИН, двигаем
    курсор, пересчитываем находки из полного кэша. В долги не пишем."""
    cache = _read_json(db, GISGMP_CACHE_KEY, {})
    if not isinstance(cache, dict):
        cache = {}

    received = 0
    for ch in charges:
        uin = (ch.get("uin") or "").strip()
        if not uin:
            continue
        cache[uin] = {k: ch.get(k) for k in _CHARGE_FIELDS}
        received += 1

    # Потолок кэша: оставляем самые свежие по дате актуализации.
    if len(cache) > _CACHE_CAP:
        items = sorted(
            cache.items(),
            key=lambda kv: parse_reg_dt(kv[1].get("actualize_date")) or datetime.min,
            reverse=True,
        )
        cache = dict(items[:_CACHE_CAP])
    _write_json(db, GISGMP_CACHE_KEY, cache, "Кэш начислений ГИС ГМП по УИН")

    # Курсор инкремента = макс дата актуализации в кэше.
    mx = None
    for ch in cache.values():
        dt = parse_reg_dt(ch.get("actualize_date"))
        if dt and (mx is None or dt > mx):
            mx = dt
    _write_json(db, GISGMP_CURSOR_KEY, {"since": mx.isoformat() if mx else None},
                "Курсор ГИС ГМП (макс дата актуализации)")

    findings = _recompute_findings(db, cache)
    db.commit()

    result = {
        "status": "ok",
        "received": received,
        "cache_total": len(cache),
        "residents": findings["residents"],
        "matched": findings["matched"],
        "not_found": findings["not_found"],
        "diag": findings["diag"],
    }
    logger.info("[GISGMP] incremental sync: %s", result)
    return result
