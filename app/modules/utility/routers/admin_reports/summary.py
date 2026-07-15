# Финансовые сводки: summary v1, диагностика/репейр холостяцких квартир,
# summary v2 (KPI, Δ, sparkline, фин-флаги, группировка по жильцам/квартирам).
# Вербатим-перенос из admin_reports.py (строки 527-1356), поведение 1:1.

from decimal import Decimal
from typing import Optional

from fastapi import Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.utility.models import User, MeterReading, BillingPeriod, Room
from app.modules.utility.services.period_helpers import period_chron_key

from ._shared import _report_group, _unit_label, router


@router.get("/api/admin/summary")
async def get_accountant_summary(
        period_id: Optional[int] = Query(None),
        current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    if current_user.role not in ("accountant", "admin"): raise HTTPException(status_code=403, detail="Доступ запрещен")

    # ИСПРАВЛЕНИЕ: Добавляем yield_per, чтобы не грузить весь массив в RAM разом.
    stmt = (
        select(User, MeterReading, Room)
        .join(MeterReading, User.id == MeterReading.user_id)
        .join(Room, User.room_id == Room.id)
        .where(MeterReading.is_approved.is_(True))
    ).execution_options(yield_per=1000)

    if period_id:
        stmt = stmt.where(MeterReading.period_id == period_id)
    else:
        last_period = (
            await db.execute(select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1))).scalars().first()
        if not last_period: return {}
        stmt = stmt.where(MeterReading.period_id == last_period.id)

    stmt = stmt.order_by(Room.dormitory_name, Room.room_number, User.username)

    summary = {}

    # Потоковое получение
    result = await db.stream(stmt)
    async for row in result:
        user, reading, room = row
        dorm = _report_group(room)
        if dorm not in summary: summary[dorm] = []
        summary[dorm].append({
            "reading_id": reading.id, "user_id": user.id, "username": user.username, "area": room.apartment_area,
            "residents": room.total_room_residents or 1, "hot": reading.cost_hot_water or 0, "cold": reading.cost_cold_water or 0,
            "sewage": reading.cost_sewage or 0, "electric": reading.cost_electricity or 0,
            "maintenance": reading.cost_maintenance or 0, "rent": reading.cost_social_rent or 0,
            "waste": reading.cost_waste or 0, "fixed": reading.cost_fixed_part or 0,
            "total_cost": reading.total_cost or 0, "total_209": reading.total_209 or 0,
            "total_205": reading.total_205 or 0,
            "date": reading.created_at.strftime("%Y-%m-%d %H:%M")
        })

    return summary


# =====================================================================
# SUMMARY v2 — расширенная сводка с финансовыми анализаторами
# =====================================================================
# Отличия от v1 (/api/admin/summary):
#   * Группировка по общежитиям + KPI на верхнем уровне
#   * Δ vs прошлый период для каждого жильца (рост/падение суммы)
#   * Sparkline за последние 6 периодов для каждого жильца
#   * Финансовые флаги от finance_analyzer (DEBT_GROWING и т.д.)
#   * Список MISSING_RECEIPT — жильцы без квитанции в этом периоде
#   * Поддержка фильтров: only_debtors, only_anomaly, only_overpaid, search
#
# Используется новой версткой «Финансовая отчётность» в админке.
@router.get("/api/admin/diag/singles-dupes",
            summary="Диагностика: дубли ФИО/показаний + конфиг холостяцких квартир")
async def diag_singles_dupes(
    period_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """READ-ONLY. Помогает понять почему холостяки не делятся и откуда дубли ФИО.

    Возвращает:
      - duplicate_users: одинаковое ФИО у 2+ активных Л/С в ОДНОЙ комнате
        (это и есть «дубли ФИО» — две учётки одного человека/квартиры);
      - duplicate_readings: у жильца 2+ утверждённых показания за период
        (мусорные строки в отчёте);
      - singles_rooms: комнаты is_singles_apartment=True с total_room_residents
        vs факт. число активных Л/С (mismatch → делитель счёта неверный);
      - singles_candidates: комнаты НЕ помеченные холостяцкими, но с 2+ Л/С или
        жильцами resident_type='single' — кандидаты на отметку «холостяцкая».
    """
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(403, "Доступ запрещён")

    if period_id:
        period = await db.get(BillingPeriod, period_id)
    else:
        period = (await db.execute(
            select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1)
        )).scalars().first()

    # Все активные жильцы (одним запросом) — группируем в Python (DB-agnostic).
    urows = (await db.execute(
        select(User.id, User.username, User.room_id,
               User.resident_type, User.billing_mode)
        .where(User.is_deleted.is_(False), User.role == "user")
    )).all()

    by_room: dict = {}
    for uid, uname, rid, rtype, bmode in urows:
        if rid is None:
            continue
        by_room.setdefault(rid, []).append(
            {"id": uid, "username": uname, "resident_type": rtype, "billing_mode": bmode})

    # Комнаты — конфиг.
    rrows = (await db.execute(
        select(Room.id, Room.room_number, Room.dormitory_name,
               Room.is_singles_apartment, Room.total_room_residents, Room.max_capacity)
    )).all()
    room_cfg = {r[0]: {"room_number": r[1], "dormitory": r[2],
                       "is_singles_apartment": bool(r[3]),
                       "total_room_residents": r[4], "max_capacity": r[5]}
                for r in rrows}

    duplicate_users = []
    singles_rooms = []
    singles_candidates = []
    for rid, residents in by_room.items():
        cfg = room_cfg.get(rid, {})
        # дубли ФИО в комнате
        seen: dict = {}
        for u in residents:
            seen.setdefault((u["username"] or "").strip().lower(), []).append(u)
        for key, grp in seen.items():
            if len(grp) > 1:
                duplicate_users.append({
                    "room_id": rid, "room_number": cfg.get("room_number"),
                    "dormitory": cfg.get("dormitory"),
                    "username": grp[0]["username"],
                    "user_ids": [g["id"] for g in grp], "count": len(grp),
                })
        cnt = len(residents)
        has_single = any(u["resident_type"] == "single" for u in residents)
        if cfg.get("is_singles_apartment"):
            singles_rooms.append({
                "room_id": rid, "room_number": cfg.get("room_number"),
                "dormitory": cfg.get("dormitory"),
                "total_room_residents": cfg.get("total_room_residents"),
                "active_residents": cnt,
                "max_capacity": cfg.get("max_capacity"),
                "headcount_mismatch": (cfg.get("total_room_residents") or 0) != cnt,
            })
        elif cnt > 1 or has_single:
            singles_candidates.append({
                "room_id": rid, "room_number": cfg.get("room_number"),
                "dormitory": cfg.get("dormitory"),
                "active_residents": cnt,
                "has_single_type": has_single,
                "usernames": [u["username"] for u in residents],
            })

    duplicate_readings = []
    if period:
        dr = (await db.execute(
            select(MeterReading.user_id, func.count(MeterReading.id),
                   func.min(MeterReading.id), func.max(MeterReading.id))
            .where(MeterReading.is_approved.is_(True),
                   MeterReading.period_id == period.id)
            .group_by(MeterReading.user_id)
            .having(func.count(MeterReading.id) > 1)
        )).all()
        uname_by_id = {u[0]: u[1] for u in urows}
        for uid, c, mn, mx in dr:
            duplicate_readings.append({
                "user_id": uid, "username": uname_by_id.get(uid),
                "approved_count": int(c), "min_reading_id": mn, "max_reading_id": mx,
            })

    return {
        "period": ({"id": period.id, "name": period.name} if period else None),
        "summary": {
            "duplicate_user_groups": len(duplicate_users),
            "duplicate_reading_users": len(duplicate_readings),
            "singles_rooms": len(singles_rooms),
            "singles_rooms_mismatched": sum(1 for s in singles_rooms if s["headcount_mismatch"]),
            "singles_candidates": len(singles_candidates),
        },
        "duplicate_users": sorted(duplicate_users, key=lambda x: (x["dormitory"] or "", x["room_number"] or "")),
        "duplicate_readings": duplicate_readings,
        "singles_rooms": sorted(singles_rooms, key=lambda x: (x["dormitory"] or "", x["room_number"] or "")),
        "singles_candidates": sorted(singles_candidates, key=lambda x: (x["dormitory"] or "", x["room_number"] or "")),
    }


@router.post("/api/admin/diag/fix-singles",
             summary="Холостяки: пересчитать делитель счётчиков + выровнять доли поровну за период")
async def fix_singles(
    period_id: Optional[int] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """РЕПЕЙР (мутирует биллинг, только админ). Для КАЖДОЙ холостяцкой квартиры:
    1) пересчитывает total_room_residents = число активных Л/С (делитель счёта);
    2) за указанный/активный период выравнивает квитанции жильцов: берёт общий
       счётчик квартиры (показание с макс. суммой), считает долю одного человека
       и раскидывает её РАВНО на всех жильцов. Сальдо 1С не трогает.

    Нужно после включения холостяцкого режима / правок, когда у жильцов остались
    НЕзависимые показания (один с расходом, другой baseline) — приводит к
    «начислять всем поровну»."""
    if current_user.role != "admin":
        raise HTTPException(403, "Только админ")

    if period_id:
        period = await db.get(BillingPeriod, period_id)
    else:
        period = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active)
        )).scalars().first()
    if not period:
        raise HTTPException(400, "Период не найден")

    from app.modules.utility.services.room_assignment import recount_singles_residents
    from app.modules.utility.services.singles_billing import equalize_singles_room

    rooms = (await db.execute(
        select(Room).where(Room.is_singles_apartment.is_(True))
    )).scalars().all()

    recounted = 0
    results = []
    for room in rooms:
        old_trr = room.total_room_residents
        await recount_singles_residents(db, room.id)
        await db.flush()
        if room.total_room_residents != old_trr:
            recounted += 1
        try:
            res = await equalize_singles_room(db, room=room, period_id=period.id)
        except Exception as ex:  # noqa: BLE001
            res = {"room_id": room.id, "status": "error", "reason": str(ex)[:200]}
        res["old_trr"] = old_trr
        res["new_trr"] = room.total_room_residents
        results.append(res)

    await db.commit()
    return {
        "period": {"id": period.id, "name": period.name},
        "singles_rooms": len(rooms),
        "recounted_divider": recounted,
        "equalized": sum(1 for r in results if r.get("status") == "equalized"),
        "results": results,
    }


@router.get("/api/admin/summary/v2")
async def get_accountant_summary_v2(
    period_id: Optional[int] = Query(None),
    only_debtors: bool = Query(False),
    only_overpaid: bool = Query(False),
    only_anomaly: bool = Query(False),
    only_missing: bool = Query(False),
    search: Optional[str] = Query(None),
    # housing_001/E2-C: фильтры по типу помещения. Когда админ хочет
    # увидеть финотчёт ТОЛЬКО по домам/общагам или по конкретной улице.
    # На начислении не отражается — это только сужение выборки.
    place_type: Optional[str] = Query(
        None, pattern="^(dormitory|house)$",
        description="dormitory | house — фильтр по типу помещения",
    ),
    street: Optional[str] = Query(
        None, description="Точное название улицы (для домов)",
    ),
    history_periods: int = Query(
        12,
        ge=1,
        le=12,
        description="Сколько ПРЕДЫДУЩИХ периодов показать в истории жильца "
                    "(1-12). По умолчанию 12 — год истории. Кап 12 (был 24): "
                    "загрузка 2 лет истории в память — DoS-вектор (security-аудит)."
    ),
    group_by: str = Query(
        "user", pattern="^(user|room)$",
        description="user — по жильцам (ФИО), room — по квартирам (адрес, "
                    "агрегат по всем жильцам комнаты). Финсводка следит за "
                    "помещениями, а не за людьми.",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if current_user.role not in ("accountant", "admin"):
        raise HTTPException(403, "Доступ запрещён")

    from app.modules.utility.services.finance_analyzer import (
        analyze_finance, FLAG_CATALOG,
    )

    # 1) Период
    if period_id:
        period = await db.get(BillingPeriod, period_id)
    else:
        period = (await db.execute(
            select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1)
        )).scalars().first()
    if not period:
        return {"group_by": group_by, "period": None, "kpi": {}, "dormitories": [], "flag_catalog": FLAG_CATALOG}

    # 2) Тянем все утверждённые показания за этот период + жильцов с комнатой
    stmt = (
        select(User, MeterReading, Room)
        .join(MeterReading, User.id == MeterReading.user_id)
        .join(Room, User.room_id == Room.id)
        .where(
            MeterReading.is_approved.is_(True),
            MeterReading.period_id == period.id,
            User.is_deleted.is_(False),
        )
        .order_by(Room.dormitory_name, Room.room_number, User.username)
    )
    # housing_001/E2-C: фильтры по типу помещения сужают выборку
    # ДО группировки по домам.
    if place_type:
        stmt = stmt.where(Room.place_type == place_type)
    if street:
        stmt = stmt.where(Room.street == street)
    # Анти-дубль строк (2026-06-17): если у жильца за период оказалось НЕСКОЛЬКО
    # утверждённых reading'ов (исторический мусор/двойной импорт) — берём только
    # ПОСЛЕДНИЙ (max id), иначе ФИО дублируется в отчёте. Дубли РАЗНЫХ user_id с
    # одинаковым ФИО (две учётки в комнате) при этом остаются видны — это
    # реальная проблема данных, её надо чистить отдельно (см. diag-эндпоинт).
    latest_reading_ids = (
        select(func.max(MeterReading.id))
        .where(
            MeterReading.is_approved.is_(True),
            MeterReading.period_id == period.id,
        )
        .group_by(MeterReading.user_id)
        .scalar_subquery()
    )
    stmt = stmt.where(MeterReading.id.in_(latest_reading_ids))
    rows = (await db.execute(stmt)).all()

    # 3) История за N предыдущих периодов — для sparkline и Δ.
    # N приходит query-параметром history_periods (default 12 = год).
    # Раньше было захардкожено 6; админ хотел видеть глубже — сделали
    # настраиваемым. Берём ВСЕ utility-readings этих жильцов одним
    # запросом, фильтруем в Python: дешевле чем N запросов по жильцам.
    #
    # ВАЖНО: «прошлые» периоды определяем по БИЛЛИНГОВОЙ хронологии
    # (parsed name → year, month), а не по `BillingPeriod.id`. Иначе
    # подача задним числом (Февраль 2026 импортирован в мае) ломает
    # сортировку и считает Δ относительно «не того» предыдущего. См.
    # инцидент мая 2026 с Сорокиным С.А. и helper period_helpers.py.
    user_ids = [r[0].id for r in rows]
    history_map: dict[int, list[MeterReading]] = {uid: [] for uid in user_ids}
    # period_id → chronological_key. Хоистим дефолтом, чтобы room-режим мог
    # безопасно ссылаться даже когда история пустая.
    period_id_to_key: dict = {}
    if user_ids:
        all_periods = (await db.execute(select(BillingPeriod))).scalars().all()
        cur_key = period_chron_key(period.name)
        # Строго раньше текущего по хронологии (для Δ нужны только прошлые).
        prev_periods_sorted = sorted(
            (p for p in all_periods if period_chron_key(p.name) < cur_key),
            key=lambda p: period_chron_key(p.name),
            reverse=True,  # DESC — самый свежий первый
        )[:history_periods]
        prev_period_ids = [p.id for p in prev_periods_sorted]
        # period_id → chronological_key — для сортировки readings ниже.
        period_id_to_key = {p.id: period_chron_key(p.name) for p in prev_periods_sorted}

        if prev_period_ids:
            hist_rows = (await db.execute(
                select(MeterReading)
                .where(
                    MeterReading.user_id.in_(user_ids),
                    MeterReading.period_id.in_(prev_period_ids),
                    MeterReading.is_approved.is_(True),
                )
            )).scalars().all()
            # Сортируем по БИЛЛИНГОВОЙ хронологии ASC (старые → новые).
            # `prev_costs[-1]` будет САМЫМ СВЕЖИМ прошлым → правильная Δ.
            # Раньше сортировали по `period_id` — задним-числом импорт давал
            # «прошлым» Февральскую призрачную подачу с миллионными cost.
            tmp: dict[int, list] = {}
            for hr in hist_rows:
                tmp.setdefault(hr.user_id, []).append(hr)
            for uid in user_ids:
                history_map[uid] = sorted(
                    tmp.get(uid, []),
                    key=lambda r: period_id_to_key.get(r.period_id, (0, 0)),
                )

    # 4) MISSING_RECEIPT — жильцы с комнатой, но без MeterReading в этом периоде.
    missing_users = []
    if not only_debtors and not only_overpaid and not only_anomaly:
        # Только если фильтры не отсекают этот класс жильцов
        all_residents = (await db.execute(
            select(User, Room)
            .join(Room, User.room_id == Room.id)
            .where(
                User.is_deleted.is_(False),
                User.role == "user",
            )
        )).all()
        present = {r[0].id for r in rows}
        for user, room in all_residents:
            if user.id in present:
                continue
            if search and search.lower() not in (user.username or "").lower() \
                    and search not in (room.room_number or ""):
                continue
            missing_users.append((user, room))

    # ============================================================
    # РЕЖИМ «КВАРТИРЫ» (group_by=room): агрегируем по помещению, а не по
    # жильцу. Долг квартиры = сумма по ВСЕМ её жильцам (решение 30.05.2026).
    # Фин-фильтры (only_debtors/overpaid/anomaly/missing) применяются на
    # УРОВНЕ комнаты после суммирования, не по каждому жильцу. Внутри
    # каждой комнаты несём список жильцов — для разворота строки.
    # ============================================================
    if group_by == "room":
        from app.modules.utility.services.anomaly_flags import real_flags

        room_acc: dict[int, dict] = {}

        def _ensure_room(room) -> dict:
            if room.id not in room_acc:
                room_acc[room.id] = {
                    "room_id": room.id,
                    "dorm_name": _report_group(room),
                    "room_number": _unit_label(room),
                    "address": room.format_address or (room.room_number or "—"),
                    "place_type": getattr(room, "place_type", None),
                    "area": float(room.apartment_area or 0),
                    "residents_count": 0,
                    "is_singles_apartment": bool(getattr(room, "is_singles_apartment", False)),
                    "max_capacity": int(room.max_capacity) if getattr(room, "max_capacity", None) else None,
                    "total_cost": Decimal("0"),
                    "total_209": Decimal("0"),
                    "total_205": Decimal("0"),
                    "debt": Decimal("0"),
                    "overpayment": Decimal("0"),
                    "anomaly_score": 0,
                    "meter_flags": set(),
                    "missing_count": 0,
                    "reading_ids": [],
                    "residents": [],
                    "_cost_by_key": {},   # chrono_key -> сумма cost жильцов
                    "_debt_by_key": {},   # chrono_key -> сумма debt жильцов
                }
            return room_acc[room.id]

        # 1) Суммируем поданные показания по комнатам.
        for user, reading, room in rows:
            if search:
                s = search.lower().strip()
                if s not in (user.username or "").lower() and s not in (room.room_number or ""):
                    continue
            debt = Decimal(str((reading.debt_209 or 0) + (reading.debt_205 or 0)))
            overpay = Decimal(str((reading.overpayment_209 or 0) + (reading.overpayment_205 or 0)))
            cur_cost = Decimal(str(reading.total_cost or 0))
            ra = _ensure_room(room)
            ra["residents_count"] += 1
            ra["total_cost"] += cur_cost
            ra["total_209"] += Decimal(str(reading.total_209 or 0))
            ra["total_205"] += Decimal(str(reading.total_205 or 0))
            ra["debt"] += debt
            ra["overpayment"] += overpay
            ra["anomaly_score"] = max(ra["anomaly_score"], int(reading.anomaly_score or 0))
            for mf in real_flags(reading.anomaly_flags):
                ra["meter_flags"].add(mf)
            if reading.id:
                ra["reading_ids"].append(reading.id)
            for h in history_map.get(user.id, []):
                k = period_id_to_key.get(h.period_id)
                if k is None:
                    continue
                ra["_cost_by_key"][k] = ra["_cost_by_key"].get(k, Decimal("0")) + Decimal(str(h.total_cost or 0))
                ra["_debt_by_key"][k] = ra["_debt_by_key"].get(k, Decimal("0")) + Decimal(str((h.debt_209 or 0) + (h.debt_205 or 0)))
            ra["residents"].append({
                "user_id": user.id,
                "username": user.username,
                "residents_count": room.total_room_residents or 1,
                "reading_id": reading.id,
                "total_cost": float(cur_cost),
                "debt": float(debt),
                "overpayment": float(overpay),
            })

        # 2) Жильцы без подачи (MISSING_RECEIPT) — помечаем их комнату.
        for user, room in missing_users:
            ra = _ensure_room(room)
            ra["missing_count"] += 1
            ra["residents"].append({
                "user_id": user.id,
                "username": user.username,
                "residents_count": room.total_room_residents or 1,
                "reading_id": None,
                "total_cost": 0.0,
                "debt": 0.0,
                "overpayment": 0.0,
            })

        # 3) Финализация: re-analyze по агрегату комнаты, флаги, фильтры.
        rooms_by_dorm: dict[str, dict] = {}
        grand_billed_r = Decimal("0")
        grand_debt_r = Decimal("0")
        grand_overpay_r = Decimal("0")
        flagged_rooms = 0
        missing_rooms = 0
        all_rooms_flat = []

        for ra in room_acc.values():
            keys_sorted = sorted(ra["_cost_by_key"].keys())
            prev_costs = [ra["_cost_by_key"][k] for k in keys_sorted]
            prev_debts = [ra["_debt_by_key"].get(k, Decimal("0")) for k in keys_sorted]
            cur_cost = ra["total_cost"]
            debt = ra["debt"]
            overpay = ra["overpayment"]
            has_reading = bool(ra["reading_ids"])

            flags, fin_score = analyze_finance(
                user_id=ra["room_id"],
                residents_count=ra["residents_count"] or 1,
                current_total_cost=cur_cost,
                current_debt=debt,
                current_overpayment=overpay,
                prev_costs=prev_costs,
                prev_debts=prev_debts,
                has_reading=has_reading,
            )
            if ra["missing_count"] and not has_reading and "MISSING_RECEIPT" not in flags:
                flags = list(flags) + ["MISSING_RECEIPT"]
            meter_flags = sorted(ra["meter_flags"])

            # Фин-фильтры на уровне комнаты
            if only_debtors and debt <= 0:
                continue
            if only_overpaid and overpay <= 0:
                continue
            if only_anomaly and not flags and not meter_flags:
                continue
            if only_missing and not ra["missing_count"]:
                continue

            delta_amount = None
            delta_percent = None
            if prev_costs:
                last = prev_costs[-1]
                delta_amount = float(cur_cost - last)
                if last > 0:
                    delta_percent = float((cur_cost - last) / last * 100)
            sparkline = [float(c) for c in prev_costs] + [float(cur_cost)]

            row = {
                "room_id": ra["room_id"],
                "room_number": ra["room_number"],
                "address": ra["address"],
                "place_type": ra["place_type"],
                "area": ra["area"],
                "residents_count": ra["residents_count"],
                "missing_count": ra["missing_count"],
                "total_cost": float(cur_cost),
                "total_209": float(ra["total_209"]),
                "total_205": float(ra["total_205"]),
                "debt": float(debt),
                "overpayment": float(overpay),
                "delta_amount": delta_amount,
                "delta_percent": delta_percent,
                "sparkline": sparkline,
                "finance_flags": flags,
                "finance_score": fin_score,
                "meter_flags": meter_flags,
                "anomaly_score": ra["anomaly_score"],
                "reading_ids": ra["reading_ids"],
                "residents": sorted(ra["residents"], key=lambda r: (r.get("username") or "")),
            }
            dn = ra["dorm_name"]
            if dn not in rooms_by_dorm:
                rooms_by_dorm[dn] = {
                    "name": dn, "rooms": [], "total_billed": Decimal("0"),
                    "total_debt": Decimal("0"), "total_overpay": Decimal("0"),
                    "flagged_count": 0,
                }
            dd = rooms_by_dorm[dn]
            dd["rooms"].append(row)
            dd["total_billed"] += cur_cost
            dd["total_debt"] += debt
            dd["total_overpay"] += overpay
            if flags or meter_flags:
                dd["flagged_count"] += 1
                flagged_rooms += 1
            if ra["missing_count"]:
                missing_rooms += 1
            grand_billed_r += cur_cost
            grand_debt_r += debt
            grand_overpay_r += overpay
            all_rooms_flat.append(row)

        dorms_out = []
        for name in sorted(rooms_by_dorm.keys()):
            dd = rooms_by_dorm[name]
            dd["rooms"].sort(key=lambda r: (r.get("room_number") or ""))
            dorms_out.append({
                "name": dd["name"],
                "total_billed": float(dd["total_billed"]),
                "total_debt": float(dd["total_debt"]),
                "total_overpay": float(dd["total_overpay"]),
                "flagged_count": dd["flagged_count"],
                "rooms_count": len(dd["rooms"]),
                "rooms": dd["rooms"],
            })

        top_debtor_rooms = sorted(
            [r for r in all_rooms_flat if r["debt"] > 0], key=lambda r: -r["debt"]
        )[:5]
        top_overpay_rooms = sorted(
            [r for r in all_rooms_flat if r["overpayment"] > 0], key=lambda r: -r["overpayment"]
        )[:5]

        return {
            "group_by": "room",
            "period": {"id": period.id, "name": period.name, "is_active": period.is_active},
            "kpi": {
                "total_billed": float(grand_billed_r),
                "total_debt": float(grand_debt_r),
                "total_overpay": float(grand_overpay_r),
                "flagged_count": flagged_rooms,
                "rooms_count": sum(d["rooms_count"] for d in dorms_out),
                "missing_count": missing_rooms,
            },
            "top_debtors": top_debtor_rooms,
            "top_overpayers": top_overpay_rooms,
            "dormitories": dorms_out,
            "flag_catalog": FLAG_CATALOG,
        }

    # 5) Сборка ответа
    grand_billed = Decimal("0")
    grand_debt = Decimal("0")
    grand_overpay = Decimal("0")
    flagged_count = 0

    by_dorm: dict[str, dict] = {}

    def _ensure_dorm(name: str) -> dict:
        if name not in by_dorm:
            by_dorm[name] = {
                "name": name,
                "residents": [],
                "total_billed": Decimal("0"),
                "total_debt": Decimal("0"),
                "total_overpay": Decimal("0"),
                "flagged_count": 0,
            }
        return by_dorm[name]

    for user, reading, room in rows:
        debt = (reading.debt_209 or 0) + (reading.debt_205 or 0)
        overpay = (reading.overpayment_209 or 0) + (reading.overpayment_205 or 0)
        debt = Decimal(str(debt))
        overpay = Decimal(str(overpay))
        cur_cost = Decimal(str(reading.total_cost or 0))

        # Поиск
        if search:
            s = search.lower().strip()
            if s not in (user.username or "").lower() and s not in (room.room_number or ""):
                continue

        # Фильтры
        if only_debtors and debt <= 0:
            continue
        if only_overpaid and overpay <= 0:
            continue

        history = history_map.get(user.id, [])
        prev_costs = [Decimal(str(h.total_cost or 0)) for h in history]
        prev_debts = [
            Decimal(str((h.debt_209 or 0) + (h.debt_205 or 0)))
            for h in history
        ]

        flags, fin_score = analyze_finance(
            user_id=user.id,
            residents_count=room.total_room_residents or 1,
            current_total_cost=cur_cost,
            current_debt=debt,
            current_overpayment=overpay,
            prev_costs=prev_costs,
            prev_debts=prev_debts,
            has_reading=True,
        )

        # Только настоящие аномалии — без source-маркеров (GSHEETS_AUTO,
        # AUTO_GENERATED, DATA_OVERFLOW_RESET и т.п.). Раньше KPI «Аномалий»
        # включал все записи с anomaly_flags!=NULL — даже служебные, и
        # показывал сотни проблем при том что filter «Аномалии» находил
        # единицы. См. services/anomaly_flags.py:SOURCE_MARKERS.
        from app.modules.utility.services.anomaly_flags import real_flags
        meter_flags = real_flags(reading.anomaly_flags)

        if only_anomaly and not flags and not meter_flags:
            continue

        # Δ vs прошлый
        delta_amount = None
        delta_percent = None
        if prev_costs:
            last = prev_costs[-1]
            delta_amount = float(cur_cost - last)
            if last > 0:
                delta_percent = float((cur_cost - last) / last * 100)

        # Sparkline: 6 точек (включая текущий — последняя)
        sparkline = [float(c) for c in prev_costs] + [float(cur_cost)]

        d = _ensure_dorm(_report_group(room))
        d["residents"].append({
            "user_id": user.id,
            "username": user.username,
            "room_number": _unit_label(room),
            "room_id": room.id,
            "area": float(room.apartment_area or 0),
            "residents_count": room.total_room_residents or 1,
            # Признаки холостяцкой квартиры — для группировки в отчёте по
            # квартирам (равные доли). resident_type — бейдж «(хол.)».
            "is_singles_apartment": bool(getattr(room, "is_singles_apartment", False)),
            "resident_type": getattr(user, "resident_type", "family"),
            "max_capacity": int(room.max_capacity) if getattr(room, "max_capacity", None) else None,
            "reading_id": reading.id,
            "total_cost": float(cur_cost),
            "total_209": float(reading.total_209 or 0),
            "total_205": float(reading.total_205 or 0),
            "debt": float(debt),
            "overpayment": float(overpay),
            "delta_amount": delta_amount,
            "delta_percent": delta_percent,
            "sparkline": sparkline,
            "finance_flags": flags,
            "finance_score": fin_score,
            "meter_flags": meter_flags,
            "anomaly_score": int(reading.anomaly_score or 0),
            "created_at": reading.created_at.isoformat() if reading.created_at else None,
        })
        d["total_billed"] += cur_cost
        d["total_debt"] += debt
        d["total_overpay"] += overpay
        if flags or meter_flags:
            d["flagged_count"] += 1
            flagged_count += 1
        grand_billed += cur_cost
        grand_debt += debt
        grand_overpay += overpay

    # MISSING_RECEIPT добавляем как «жильцов без подачи»
    if missing_users and not only_anomaly and not only_debtors and not only_overpaid:
        for user, room in missing_users:
            d = _ensure_dorm(_report_group(room))
            d["residents"].append({
                "user_id": user.id,
                "username": user.username,
                "room_number": _unit_label(room),
                "room_id": room.id,
                "area": float(room.apartment_area or 0),
                "residents_count": room.total_room_residents or 1,
                "is_singles_apartment": bool(getattr(room, "is_singles_apartment", False)),
                "resident_type": getattr(user, "resident_type", "family"),
                "max_capacity": int(room.max_capacity) if getattr(room, "max_capacity", None) else None,
                "reading_id": None,
                "total_cost": 0.0,
                "total_209": 0.0,
                "total_205": 0.0,
                "debt": 0.0,
                "overpayment": 0.0,
                "delta_amount": None,
                "delta_percent": None,
                "sparkline": [],
                "finance_flags": ["MISSING_RECEIPT"],
                "finance_score": 40,
                "meter_flags": [],
                "anomaly_score": 0,
                "created_at": None,
            })
            # flagged_count для missing-receipt НЕ инкрементим — у них есть
            # своя отдельная KPI «Без квитанции» (missing_count). Иначе KPI
            # «Аномалий» = реальные + missing, и расходится с фильтром
            # «Аномалии», который missing игнорирует.

    if only_missing:
        # отфильтровываем всё кроме MISSING_RECEIPT
        for dn in list(by_dorm.keys()):
            by_dorm[dn]["residents"] = [
                r for r in by_dorm[dn]["residents"]
                if "MISSING_RECEIPT" in (r.get("finance_flags") or [])
            ]
            if not by_dorm[dn]["residents"]:
                del by_dorm[dn]

    # Сортируем общежития по имени, жильцов внутри — по комнате
    dormitories_out = []
    for name in sorted(by_dorm.keys()):
        d = by_dorm[name]
        d["residents"].sort(key=lambda r: (r.get("room_number") or "", r.get("username") or ""))
        dormitories_out.append({
            "name": d["name"],
            "total_billed": float(d["total_billed"]),
            "total_debt": float(d["total_debt"]),
            "total_overpay": float(d["total_overpay"]),
            "flagged_count": d["flagged_count"],
            "residents_count": len(d["residents"]),
            "residents": d["residents"],
        })

    # Топ-должники / топ-плательщики (по всем общежитиям)
    all_residents = [r for d in dormitories_out for r in d["residents"]]
    top_debtors = sorted(
        [r for r in all_residents if r["debt"] > 0],
        key=lambda r: -r["debt"],
    )[:5]
    top_overpayers = sorted(
        [r for r in all_residents if r["overpayment"] > 0],
        key=lambda r: -r["overpayment"],
    )[:5]

    return {
        "group_by": "user",
        "period": {"id": period.id, "name": period.name, "is_active": period.is_active},
        "kpi": {
            "total_billed": float(grand_billed),
            "total_debt": float(grand_debt),
            "total_overpay": float(grand_overpay),
            "flagged_count": flagged_count,
            "residents_count": sum(d["residents_count"] for d in dormitories_out),
            "missing_count": len(missing_users),
        },
        "top_debtors": top_debtors,
        "top_overpayers": top_overpayers,
        "dormitories": dormitories_out,
        "flag_catalog": FLAG_CATALOG,
    }
