"""Выгрузка долгов 1С в показания активного периода — общий код для ручной
кнопки «Выгрузить» (financier.debts_publish) и авто-выгрузки после ежедневного
сбора 1С (tasks.onec_autopublish_task).

1С — ЕДИНСТВЕННЫЙ источник долгов (ГИС ГМП не перебивает). Полная замена по
выгружаемому счёту: кого нет в черновике → 0 по этому счёту. Снимок «до» — для
отката через историю импортов.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from sqlalchemy import select, desc, update as _sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time_utils import utcnow
from app.modules.utility.models import (
    BillingPeriod, DebtImportLog, MeterReading, SystemSetting,
)

logger = logging.getLogger(__name__)

# Зеркало financier.ONEC_RELAY_KEY — сюда пишем статус авто-выгрузки, чтобы НЕ
# тащить роутер в celery-воркер (онлайн-статус 1С и так в этом ключе).
ONEC_RELAY_KEY = "onec_relay"

# Предохранитель авто-выгрузки: не дать битому сбору (как баг парсинга, что
# обнулил 98% долгов) затереть реальные долги. Срабатывает только при заметной
# базе (≥ GUARD_MIN_PREV ненулевых) и доле обнуления ≥ GUARD_ZERO_FRACTION.
GUARD_MIN_PREV = 20
GUARD_ZERO_FRACTION = 0.5
_EPS = Decimal("0.01")


async def publish_onec_debts(db: AsyncSession, *, guard: bool = False) -> dict:
    """Берёт ПОСЛЕДНИЕ staged-черновики 1С (209/205) → пишет долги/переплаты в
    показания активного периода. НЕ бросает HTTPException (слой сервиса).

    guard=True (авто-выгрузка): если сбор выглядит аномально (обнулил бы массу
    ненулевых долгов), выгрузка ПРОПУСКАЕТСЯ — черновик остаётся staged на ручную
    проверку.

    Возвращает {"ok": bool, "status": ...}: published | no_active_period |
    no_staged | guard_tripped.
    """
    ap = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active.is_(True))
    )).scalars().first()
    if ap is None:
        return {"ok": False, "status": "no_active_period"}

    staged = {}
    for acc in ("209", "205"):
        log = (await db.execute(
            select(DebtImportLog).where(
                DebtImportLog.account_type == acc,
                DebtImportLog.status == "staged",
            ).order_by(desc(DebtImportLog.started_at)).limit(1)
        )).scalars().first()
        if log:
            staged[acc] = log
    if not staged:
        return {"ok": False, "status": "no_staged"}
    accts = set(staged.keys())

    # Целевые долги по жильцам из черновиков (applied_state).
    target: dict[int, dict] = {}
    for acc, log in staged.items():
        for uid_s, st in (log.applied_state or {}).items():
            if not str(uid_s).isdigit():
                continue
            uid = int(uid_s)
            t = target.setdefault(uid, {"room_id": st.get("room_id")})
            t[f"debt_{acc}"] = Decimal(str(st.get(f"debt_{acc}") or 0))
            t[f"over_{acc}"] = Decimal(str(st.get(f"overpayment_{acc}") or 0))
            if st.get("room_id"):
                t["room_id"] = st.get("room_id")

    # Существующие показания активного периода.
    readings = (await db.execute(
        select(MeterReading).where(MeterReading.period_id == ap.id)
    )).scalars().all()
    by_user = {r.user_id: r for r in readings if r.user_id is not None}

    # --- Предохранитель: битый сбор не должен обнулить долги жильцов ---
    if guard:
        prev_nonzero = [
            uid for uid, r in by_user.items()
            if (Decimal(str(r.debt_209 or 0)) + Decimal(str(r.debt_205 or 0))) > _EPS
        ]
        would_zero = sum(
            1 for uid in prev_nonzero
            if (target.get(uid, {}).get("debt_209", Decimal("0"))
                + target.get(uid, {}).get("debt_205", Decimal("0"))) <= _EPS
        )
        n_prev = len(prev_nonzero)
        if n_prev >= GUARD_MIN_PREV and would_zero / n_prev >= GUARD_ZERO_FRACTION:
            logger.warning(
                "[onec_publish] ПРЕДОХРАНИТЕЛЬ: сбор обнулил бы %s/%s ненулевых долгов "
                "(≥ %.0f%%) — авто-выгрузка ПРОПУЩЕНА, черновик оставлен на ручную проверку.",
                would_zero, n_prev, GUARD_ZERO_FRACTION * 100,
            )
            return {"ok": False, "status": "guard_tripped",
                    "prev_nonzero": n_prev, "would_zero": would_zero}

    snapshot_before: dict = {}
    updates: list = []
    for uid, r in by_user.items():
        vals = {}
        for acc in accts:
            vals[f"debt_{acc}"] = target.get(uid, {}).get(f"debt_{acc}", Decimal("0"))
            vals[f"overpayment_{acc}"] = target.get(uid, {}).get(f"over_{acc}", Decimal("0"))
        snapshot_before[str(r.id)] = {
            "debt_209": str(r.debt_209 or 0), "overpayment_209": str(r.overpayment_209 or 0),
            "debt_205": str(r.debt_205 or 0), "overpayment_205": str(r.overpayment_205 or 0),
        }
        updates.append((r.id, vals))

    # Создаём показания для жильцов из черновика без показания в периоде.
    new_objs = []
    for uid, t in target.items():
        if uid in by_user:
            continue
        new_objs.append(MeterReading(
            user_id=uid, room_id=t.get("room_id"), period_id=ap.id, is_approved=False,
            debt_209=t.get("debt_209", Decimal("0")), overpayment_209=t.get("over_209", Decimal("0")),
            debt_205=t.get("debt_205", Decimal("0")), overpayment_205=t.get("over_205", Decimal("0")),
        ))
    inserted_ids = []
    if new_objs:
        db.add_all(new_objs)
        await db.flush()
        inserted_ids = [n.id for n in new_objs]

    # Партиц-безопасная запись существующих (expunge + явный UPDATE по id).
    for r in list(by_user.values()):
        try:
            db.expunge(r)
        except Exception:
            pass
    updated = 0
    for rid, vals in updates:
        res = await db.execute(
            _sa_update(MeterReading).where(MeterReading.id == rid).values(**vals)
        )
        updated += res.rowcount or 0

    # Помечаем черновики completed + снимок до (для отката через историю импортов).
    now = utcnow().isoformat()
    for acc, log in staged.items():
        log.status = "completed"
        log.snapshot_data = {"before": snapshot_before,
                             "inserted_reading_ids": inserted_ids,
                             "published_at": now,
                             "auto": bool(guard)}
    await db.commit()

    return {"ok": True, "status": "published", "accounts": sorted(accts),
            "updated": updated, "created": len(inserted_ids), "residents": len(target)}


async def record_autopublish_status(db: AsyncSession, result: dict) -> None:
    """Пишет итог последней авто-выгрузки в onec-конфиг → видно в /onec/status и
    в UI «Долги 1С». Особенно важно при status='guard_tripped' (предохранитель
    сработал, выгрузка пропущена) — иначе битый сбор молча оставит жильцов на
    старых данных. Тихо логируем при ошибке записи (не валим выгрузку)."""
    try:
        row = (await db.execute(
            select(SystemSetting).where(SystemSetting.key == ONEC_RELAY_KEY)
        )).scalars().first()
        cfg = {}
        if row and row.value:
            try:
                cfg = json.loads(row.value)
            except Exception:
                cfg = {}
        cfg["last_autopublish"] = {
            "status": result.get("status"),
            "at": utcnow().isoformat(),
            "updated": result.get("updated"),
            "created": result.get("created"),
            "residents": result.get("residents"),
            "prev_nonzero": result.get("prev_nonzero"),
            "would_zero": result.get("would_zero"),
        }
        if row is None:
            row = SystemSetting(key=ONEC_RELAY_KEY, value="{}",
                                description="Конфиг и статус авто-подгрузки 1С")
            db.add(row)
        row.value = json.dumps(cfg, ensure_ascii=False)
        await db.commit()
    except Exception:
        logger.exception("[onec_publish] не удалось записать last_autopublish")
