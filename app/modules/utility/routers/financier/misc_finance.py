# Прочее финансовое: reset-balance жильца, сверка readings vs debts.
# Механически выделено из монолитного routers/financier.py (распил на
# пакет financier/): код перенесён дословно, поведение/пути/тексты 1:1.

from decimal import Decimal
from typing import Optional
from fastapi import Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
from app.core.database import get_db
from app.modules.utility.models import User, MeterReading, Room, DebtImportLog
from app.core.dependencies import get_current_user

from ._shared import (
    router,
    _nfu_fio,
    _resolve_view_period,
    _require_finance,
)


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

@router.get("/debts/reconcile", summary="Сверка: readings vs debts (выбранный/активный период)")
async def debts_reconcile(
    period_id: Optional[int] = Query(None, description="Период; по умолчанию активный → последний импорт → свежий"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Возвращает данные для вкладки «Сверка 1С» в Центре анализа:
      * readings_without_debts — есть reading, но в 1С долгов нет (ок, оплачено?)
      * debts_without_readings — в readings стоит долг, но reading не утверждён
      * last_import_not_found — ФИО из последнего импорта, не привязанные
      * unassigned — НЕразнесённый долг (сумма + по счетам): деньги из 1С, не
        привязанные к жильцу (ФИО нет в базе / нет комнаты). Главный сигнал
        проблемы — сколько денег «висит в воздухе».
    Период — через _resolve_view_period (работает и когда активного нет)."""
    _require_finance(current_user)

    active_period = await _resolve_view_period(db, period_id)
    if not active_period:
        return {
            "period": None,
            "readings_without_debts": [],
            "debts_without_readings": [],
            "last_import_not_found": [],
            "unassigned": {"total_debt": 0.0, "total_overpayment": 0.0, "count": 0,
                           "by_account": {"209": {"count": 0, "debt": 0.0},
                                          "205": {"count": 0, "debt": 0.0}}},
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

    # 4) Неразнесённый долг: not_found из ПОСЛЕДНИХ импортов 209/205 за период.
    #    Главный денежный сигнал — сколько 1С-долга не привязано к жильцу.
    u_total_debt = 0.0
    u_total_over = 0.0
    u_keys: set = set()
    by_account = {}
    for acct in ("209", "205"):
        log = (await db.execute(
            select(DebtImportLog).where(
                DebtImportLog.period_id == active_period.id,
                DebtImportLog.account_type == acct,
                DebtImportLog.status == "completed",
            ).order_by(desc(DebtImportLog.started_at)).limit(1)
        )).scalars().first()
        acct_debt = 0.0
        acct_cnt = 0
        for it in (log.not_found_users or []) if log else []:
            if isinstance(it, dict):
                fio = (it.get("fio") or "").strip()
                dd = float(it.get("debt") or 0)
                oo = float(it.get("overpayment") or 0)
            else:
                fio, dd, oo = str(it).strip(), 0.0, 0.0
            if not fio:
                continue
            u_keys.add(" ".join(fio.lower().split()))
            acct_debt += dd
            acct_cnt += 1
            u_total_debt += dd
            u_total_over += oo
        by_account[acct] = {"count": acct_cnt, "debt": round(acct_debt, 2)}

    return {
        "period": {"id": active_period.id, "name": active_period.name},
        "readings_without_debts": r_no_debts,
        "debts_without_readings": d_no_readings,
        "last_import_not_found": nf,
        "last_import_id": last_log.id if last_log else None,
        "unassigned": {
            "total_debt": round(u_total_debt, 2),
            "total_overpayment": round(u_total_over, 2),
            "count": len(u_keys),
            "by_account": by_account,
        },
    }
