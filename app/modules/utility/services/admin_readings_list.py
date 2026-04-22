# app/modules/utility/services/admin_readings_list.py

import logging
from decimal import Decimal
from typing import Optional
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc, asc, func, or_, Integer
from sqlalchemy.orm import selectinload

from app.modules.utility.models import User, MeterReading, BillingPeriod, Room
from app.modules.utility.constants import ANOMALY_MAP

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


def _infer_source(anomaly_flags: Optional[str]) -> str:
    """Определяет источник подачи по специальным маркерам в anomaly_flags.

    В системе нет отдельной колонки source — маркер кладётся в anomaly_flags
    при создании записи (см. admin_gsheets.py:_apply_approve,
    billing.py:close_current_period, admin_readings_manual.py).
    """
    if not anomaly_flags:
        return "user"
    markers = {
        "GSHEETS_IMPORT": "gsheets",
        "AUTO_GENERATED": "auto",
        "ONE_TIME_CHARGE": "one_time",
        "METER_REPLACEMENT": "meter_replace",
        "METER_CLOSED": "meter_replace",
    }
    for marker, src in markers.items():
        if marker in anomaly_flags:
            return src
    return "user"


async def get_paginated_readings(
        db: AsyncSession,
        page: int,
        limit: int,
        cursor_id: Optional[int],
        direction: str,
        search: Optional[str],
        anomalies_only: bool,
        sort_by: str,
        sort_dir: str,
        period_id: Optional[int] = None,
        risk_level: Optional[str] = None,
        flag_code: Optional[str] = None,
        source: Optional[str] = None,
):
    """
    Получение списка черновиков показаний для бухгалтера.

    Новые параметры (волна 1 «Реестр показаний 2.0»):
      * period_id — конкретный период (по умолчанию — активный)
      * risk_level — «clean» (<30), «suspicious» (30-80), «critical» (≥80)
      * flag_code — подстрочный поиск в anomaly_flags (например «SPIKE_HOT»)
      * source — «user» / «gsheets» / «auto» / «one_time» / «meter_replace»

    ИСПРАВЛЕНИЕ P2: COUNT запрос оптимизирован.
    Ранее: SELECT COUNT(*) FROM (SELECT ... JOIN User JOIN Room ... WHERE ...) — материализация
    всего отфильтрованного набора с JOINами как подзапрос. На партицированной таблице readings
    с миллионами строк это sequential scan.

    Теперь: Для базового случая (без поиска) COUNT идёт напрямую по readings без JOINов.
    При поиске — лёгкий COUNT с минимальными JOINами без ORDER BY и LIMIT.
    """
    # Резолвим период: если явно передан — используем его, иначе активный.
    selected_period = None
    if period_id is not None:
        selected_period = await db.get(BillingPeriod, period_id)
    if selected_period is None:
        selected_period = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active)
        )).scalars().first()

    if not selected_period:
        return {"total": 0, "page": page, "size": limit, "items": []}

    # =====================================================
    # COUNT — отдельный лёгкий запрос
    # =====================================================
    base_filter = [
        MeterReading.is_approved.is_(False),
        MeterReading.period_id == selected_period.id
    ]

    if anomalies_only:
        base_filter.append(MeterReading.anomaly_flags.isnot(None))

    # Фильтр по уровню риска
    if risk_level == "clean":
        base_filter.append(MeterReading.anomaly_score < 30)
    elif risk_level == "suspicious":
        base_filter.append(MeterReading.anomaly_score >= 30)
        base_filter.append(MeterReading.anomaly_score < 80)
    elif risk_level == "critical":
        base_filter.append(MeterReading.anomaly_score >= 80)

    # Фильтр по конкретному коду флага (подстрочный — anomaly_flags хранится CSV-строкой)
    if flag_code:
        base_filter.append(MeterReading.anomaly_flags.ilike(f"%{flag_code}%"))

    # Фильтр по источнику. Делаем через SQL-like — чтобы не тянуть все записи
    # и фильтровать в Python. Маппинг — обратный _infer_source.
    if source:
        if source == "gsheets":
            base_filter.append(MeterReading.anomaly_flags.ilike("%GSHEETS_IMPORT%"))
        elif source == "auto":
            base_filter.append(MeterReading.anomaly_flags.ilike("%AUTO_GENERATED%"))
        elif source == "one_time":
            base_filter.append(MeterReading.anomaly_flags.ilike("%ONE_TIME_CHARGE%"))
        elif source == "meter_replace":
            base_filter.append(or_(
                MeterReading.anomaly_flags.ilike("%METER_REPLACEMENT%"),
                MeterReading.anomaly_flags.ilike("%METER_CLOSED%"),
            ))
        elif source == "user":
            # Пользовательские — НЕ содержат специальных маркеров
            base_filter.append(or_(
                MeterReading.anomaly_flags.is_(None),
                ~MeterReading.anomaly_flags.ilike("%GSHEETS_IMPORT%"),
            ))
            base_filter.append(or_(
                MeterReading.anomaly_flags.is_(None),
                ~MeterReading.anomaly_flags.ilike("%AUTO_GENERATED%"),
            ))
            base_filter.append(or_(
                MeterReading.anomaly_flags.is_(None),
                ~MeterReading.anomaly_flags.ilike("%ONE_TIME_CHARGE%"),
            ))
            base_filter.append(or_(
                MeterReading.anomaly_flags.is_(None),
                ~MeterReading.anomaly_flags.ilike("%METER_REPLACEMENT%"),
            ))
            base_filter.append(or_(
                MeterReading.anomaly_flags.is_(None),
                ~MeterReading.anomaly_flags.ilike("%METER_CLOSED%"),
            ))

    if search:
        # При поиске нужен JOIN, но без ORDER BY и без выборки всех колонок
        search_fmt = f"%{search}%"
        count_query = (
            select(func.count(MeterReading.id))
            .join(User, MeterReading.user_id == User.id)
            .join(Room, MeterReading.room_id == Room.id)
            .where(
                *base_filter,
                or_(
                    User.username.ilike(search_fmt),
                    Room.dormitory_name.ilike(search_fmt),
                    Room.room_number.ilike(search_fmt)
                )
            )
        )
    else:
        # Без поиска: COUNT напрямую по readings, без JOIN — максимально быстро
        count_query = select(func.count(MeterReading.id)).where(*base_filter)

    total = (await db.execute(count_query)).scalar_one()

    # =====================================================
    # DATA — основной запрос с JOINами
    # =====================================================
    query = (
        select(MeterReading, User, Room)
        .join(User, MeterReading.user_id == User.id)
        .join(Room, MeterReading.room_id == Room.id)
        .where(*base_filter)
    )

    if search:
        search_fmt = f"%{search}%"
        query = query.where(
            or_(
                User.username.ilike(search_fmt),
                Room.dormitory_name.ilike(search_fmt),
                Room.room_number.ilike(search_fmt)
            )
        )

    # Транслируем created_at в id для Keyset Pagination
    if sort_by == "created_at":
        sort_by = "id"

    sort_col_map = {
        "id": MeterReading.id,
        "username": User.username,
        "dormitory": Room.dormitory_name,
        "total_cost": MeterReading.total_cost,
        "anomaly_score": MeterReading.anomaly_score,
    }
    sort_col = sort_col_map.get(sort_by, MeterReading.id)

    use_keyset = (sort_by == "id")

    # === ЛОГИКА ПАГИНАЦИИ ===
    if use_keyset and cursor_id is not None:
        if direction == "next":
            if sort_dir == "desc":
                query = query.where(MeterReading.id < cursor_id)
            else:
                query = query.where(MeterReading.id > cursor_id)
        else:  # prev
            if sort_dir == "desc":
                query = query.where(MeterReading.id > cursor_id)
            else:
                query = query.where(MeterReading.id < cursor_id)
    else:
        # Fallback для поиска или сложной сортировки (OFFSET)
        query = query.offset((page - 1) * limit)

    # === ЛОГИКА СОРТИРОВКИ ===
    if use_keyset and direction == "prev":
        query = query.order_by(asc(sort_col) if sort_dir == "desc" else desc(sort_col))
    else:
        query = query.order_by(desc(sort_col) if sort_dir == "desc" else asc(sort_col))

    query = query.limit(limit)
    rows = (await db.execute(query)).all()

    if use_keyset and direction == "prev":
        rows.reverse()

    if not rows:
        return {"total": total, "page": page, "size": limit, "items": []}

    # =====================================================
    # Предыдущие показания — batch-запрос
    # =====================================================
    room_ids = [row[2].id for row in rows]
    subq_max_prev = select(
        MeterReading.room_id,
        func.max(MeterReading.created_at).label("max_created")
    ).where(
        MeterReading.room_id.in_(room_ids),
        MeterReading.is_approved.is_(True)
    ).group_by(MeterReading.room_id).subquery()

    stmt_prev = select(MeterReading).join(
        subq_max_prev,
        (MeterReading.room_id == subq_max_prev.c.room_id) &
        (MeterReading.created_at == subq_max_prev.c.max_created)
    )
    prev_map = {r.room_id: r for r in (await db.execute(stmt_prev)).scalars().all()}

    # =====================================================
    # Сборка ответа
    # =====================================================
    items = []
    for current, user, room in rows:
        prev = prev_map.get(room.id)
        anomaly_details = []

        if current.anomaly_flags and current.anomaly_flags != "PENDING":
            for flag in current.anomaly_flags.split(","):
                flag = flag.strip()
                if flag:
                    label = ANOMALY_MAP.get(flag, flag)
                    anomaly_details.append({"code": flag, "label": label})

        d_hot = (current.hot_water or ZERO) - (prev.hot_water if prev else ZERO)
        d_cold = (current.cold_water or ZERO) - (prev.cold_water if prev else ZERO)
        d_elect = (current.electricity or ZERO) - (prev.electricity if prev else ZERO)

        items.append({
            "id": current.id,
            "user_id": user.id,
            "username": user.username,
            "dormitory": room.dormitory_name,
            "room_number": room.room_number,
            "hot_water": current.hot_water,
            "cold_water": current.cold_water,
            "electricity": current.electricity,
            # Бэк-совместимость: старый UI использовал cur_hot/cur_cold/cur_elect
            "cur_hot": current.hot_water,
            "cur_cold": current.cold_water,
            "cur_elect": current.electricity,
            "delta_hot": d_hot,
            "delta_cold": d_cold,
            "delta_elect": d_elect,
            "total_cost": current.total_cost,
            "total_209": current.total_209,
            "total_205": current.total_205,
            "anomaly_score": current.anomaly_score,
            "anomaly_flags": current.anomaly_flags,
            "anomaly_details": anomaly_details,
            # Волна 1: период и источник подачи — для UI-колонок/фильтров.
            "period_id": selected_period.id,
            "period_name": selected_period.name,
            "source": _infer_source(current.anomaly_flags),
            "created_at": current.created_at.isoformat() if current.created_at else None,
        })

    return {
        "total": total, "page": page, "size": limit, "items": items,
        "period": {"id": selected_period.id, "name": selected_period.name, "is_active": selected_period.is_active},
    }

async def get_readings_stats(db: AsyncSession, period_id: Optional[int] = None):
    """KPI для реестра показаний: счётчики по рискам, сумма, топ-флаги.

    Считаем по ЧЕРНОВИКАМ (is_approved=False) — именно они ждут утверждения.
    """
    selected_period = None
    if period_id is not None:
        selected_period = await db.get(BillingPeriod, period_id)
    if selected_period is None:
        selected_period = (await db.execute(
            select(BillingPeriod).where(BillingPeriod.is_active)
        )).scalars().first()

    if not selected_period:
        return {
            "period": None,
            "total": 0, "clean": 0, "suspicious": 0, "critical": 0,
            "anomalies": 0, "avg_cost": 0.0, "sum_cost": 0.0,
            "top_flags": [], "sources": {},
        }

    base = [
        MeterReading.is_approved.is_(False),
        MeterReading.period_id == selected_period.id,
    ]

    # Основной агрегат — один запрос
    agg_q = select(
        func.count(MeterReading.id),
        func.coalesce(func.sum(MeterReading.total_cost), 0),
        func.coalesce(func.avg(MeterReading.total_cost), 0),
        func.sum(
            func.cast(MeterReading.anomaly_score < 30, Integer)
        ).label("clean"),
        func.sum(
            func.cast(
                (MeterReading.anomaly_score >= 30) & (MeterReading.anomaly_score < 80),
                Integer,
            )
        ).label("suspicious"),
        func.sum(
            func.cast(MeterReading.anomaly_score >= 80, Integer)
        ).label("critical"),
        func.sum(
            func.cast(MeterReading.anomaly_flags.isnot(None), Integer)
        ).label("anomalies"),
    ).where(*base)
    agg = (await db.execute(agg_q)).one()
    total, sum_cost, avg_cost, clean, suspicious, critical, anomalies = agg

    # Топ-флагов: берём anomaly_flags строкой, разбиваем в Python
    flag_rows = (await db.execute(
        select(MeterReading.anomaly_flags).where(
            *base, MeterReading.anomaly_flags.isnot(None)
        )
    )).scalars().all()
    from collections import Counter
    flag_counter: Counter = Counter()
    for flags in flag_rows:
        if not flags or flags in ("PENDING", "AUTO_GENERATED", "GSHEETS_IMPORT",
                                   "ONE_TIME_CHARGE", "METER_REPLACEMENT", "METER_CLOSED"):
            continue
        for f in flags.split(","):
            f = f.strip()
            if f:
                flag_counter[f] += 1
    top_flags = [
        {"code": code, "label": ANOMALY_MAP.get(code, code), "count": cnt}
        for code, cnt in flag_counter.most_common(10)
    ]

    # Разбивка по источникам — пайтоновский подсчёт через _infer_source
    # (та же выборка anomaly_flags, что и для топ-флагов — повторно не грузим).
    sources = Counter()
    for flags in flag_rows:
        sources[_infer_source(flags)] += 1
    # Плюс записи без флагов (их нет в flag_rows) — это user
    null_flags = total - len(flag_rows) if total and flag_rows is not None else 0
    if null_flags > 0:
        sources["user"] += null_flags

    return {
        "period": {"id": selected_period.id, "name": selected_period.name,
                   "is_active": selected_period.is_active},
        "total": int(total or 0),
        "clean": int(clean or 0),
        "suspicious": int(suspicious or 0),
        "critical": int(critical or 0),
        "anomalies": int(anomalies or 0),
        "avg_cost": float(avg_cost or 0),
        "sum_cost": float(sum_cost or 0),
        "top_flags": top_flags,
        "sources": dict(sources),
    }


async def get_decision_context(db: AsyncSession, reading_id: int):
    """Подробный контекст для раскрывающейся панели реестра (волна 2).

    Возвращает:
      * сама запись с прежними значениями и дельтами
      * последние 4 утверждённых показания этой комнаты (история)
      * соседи: средние значения по общежитию и комнате
      * флаги подробно (label + severity) + рекомендация approve/review/reject
    """
    # Базовая запись с user/room
    stmt = (
        select(MeterReading, User, Room)
        .join(User, MeterReading.user_id == User.id)
        .join(Room, MeterReading.room_id == Room.id)
        .where(MeterReading.id == reading_id)
    )
    row = (await db.execute(stmt)).first()
    if not row:
        raise HTTPException(status_code=404, detail="Показание не найдено")
    reading, user, room = row

    # История: 4 последних утверждённых показания по этой комнате (без самой записи)
    hist = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.room_id == room.id,
            MeterReading.is_approved.is_(True),
            MeterReading.id != reading.id,
        )
        .order_by(desc(MeterReading.created_at))
        .limit(4)
    )).scalars().all()

    history = [
        {
            "id": h.id,
            "hot_water": float(h.hot_water or 0),
            "cold_water": float(h.cold_water or 0),
            "electricity": float(h.electricity or 0),
            "total_cost": float(h.total_cost or 0),
            "created_at": h.created_at.isoformat() if h.created_at else None,
        }
        for h in hist
    ]

    # Соседи: среднее по общежитию и комнате за ТЕКУЩИЙ период (для контекста)
    neighbors_q = (
        select(
            func.avg(MeterReading.hot_water),
            func.avg(MeterReading.cold_water),
            func.avg(MeterReading.electricity),
            func.avg(MeterReading.total_cost),
            func.count(MeterReading.id),
        )
        .join(Room, MeterReading.room_id == Room.id)
        .where(
            Room.dormitory_name == room.dormitory_name,
            MeterReading.period_id == reading.period_id,
            MeterReading.id != reading.id,
            MeterReading.is_approved.is_(True),
        )
    )
    n_hot, n_cold, n_elect, n_total, n_count = (await db.execute(neighbors_q)).one()
    neighbors = {
        "dormitory": room.dormitory_name,
        "sample_size": int(n_count or 0),
        "avg_hot": float(n_hot or 0),
        "avg_cold": float(n_cold or 0),
        "avg_elect": float(n_elect or 0),
        "avg_total_cost": float(n_total or 0),
    }

    # Флаги с деталями
    flags_detail = []
    if reading.anomaly_flags and reading.anomaly_flags != "PENDING":
        for f in reading.anomaly_flags.split(","):
            f = f.strip()
            if not f:
                continue
            severity = "high" if any(
                f.startswith(p) for p in ("SPIKE_", "NEGATIVE_", "HOT_GT_COLD", "COPY_NEIGHBOR")
            ) else ("medium" if any(
                f.startswith(p) for p in ("HIGH_", "ZERO_", "FROZEN_", "TREND_")
            ) else "low")
            flags_detail.append({
                "code": f,
                "label": ANOMALY_MAP.get(f, f),
                "severity": severity,
            })

    # Рекомендация: агрегированный вердикт на основе score + severity флагов
    score = reading.anomaly_score or 0
    has_high = any(fd["severity"] == "high" for fd in flags_detail)
    if score >= 80 or has_high:
        recommendation = {
            "verdict": "reject",
            "label": "Отклонить или проверить вручную",
            "reason": (
                "Высокий риск" if score >= 80
                else "Есть серьёзный флаг (" + next(fd["code"] for fd in flags_detail if fd["severity"] == "high") + ")"
            ),
            "color": "#dc2626",
        }
    elif score >= 30 or flags_detail:
        recommendation = {
            "verdict": "review",
            "label": "Проверить перед утверждением",
            "reason": f"Риск {score}/100" + (f", флагов: {len(flags_detail)}" if flags_detail else ""),
            "color": "#d97706",
        }
    else:
        recommendation = {
            "verdict": "approve",
            "label": "Можно утверждать",
            "reason": "Риск низкий, флагов нет",
            "color": "#059669",
        }

    # Предыдущее утверждённое — для расчёта дельт
    prev = hist[0] if hist else None
    d_hot = float((reading.hot_water or 0) - (prev["hot_water"] if prev else 0))
    d_cold = float((reading.cold_water or 0) - (prev["cold_water"] if prev else 0))
    d_elect = float((reading.electricity or 0) - (prev["electricity"] if prev else 0))

    return {
        "id": reading.id,
        "user": {"id": user.id, "username": user.username},
        "room": {
            "id": room.id,
            "dormitory_name": room.dormitory_name,
            "room_number": room.room_number,
            "area": float(room.apartment_area or 0),
        },
        "current": {
            "hot_water": float(reading.hot_water or 0),
            "cold_water": float(reading.cold_water or 0),
            "electricity": float(reading.electricity or 0),
            "delta_hot": d_hot,
            "delta_cold": d_cold,
            "delta_elect": d_elect,
            "total_cost": float(reading.total_cost or 0),
            "total_209": float(reading.total_209 or 0),
            "total_205": float(reading.total_205 or 0),
            "anomaly_score": int(reading.anomaly_score or 0),
            "source": _infer_source(reading.anomaly_flags),
        },
        "history": history,
        "neighbors": neighbors,
        "flags": flags_detail,
        "recommendation": recommendation,
    }


async def get_manual_state(db: AsyncSession, user_id: int):
    """Получение состояния для формы ручного ввода показаний."""
    user = (await db.execute(
        select(User).options(selectinload(User.room)).where(
            User.id == user_id,
            User.is_deleted.is_(False)
        )
    )).scalars().first()

    if not user:
        raise HTTPException(status_code=404, detail="Пользователь не найден")

    if not user.room:
        return {
            "user_id": user.id,
            "username": user.username,
            "room": None,
            "prev_hot": ZERO,
            "prev_cold": ZERO,
            "prev_elect": ZERO,
            "has_draft": False,
        }

    prev = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.room_id == user.room_id,
            MeterReading.is_approved.is_(True)
        )
        .order_by(desc(MeterReading.created_at))
        .limit(1)
    )).scalars().first()

    active_period = (await db.execute(
        select(BillingPeriod).where(BillingPeriod.is_active)
    )).scalars().first()

    draft = None
    if active_period:
        draft = (await db.execute(
            select(MeterReading).where(
                MeterReading.room_id == user.room_id,
                MeterReading.period_id == active_period.id,
                MeterReading.is_approved.is_(False)
            )
        )).scalars().first()

    return {
        "user_id": user.id,
        "username": user.username,
        "room": {
            "id": user.room.id,
            "dormitory_name": user.room.dormitory_name,
            "room_number": user.room.room_number,
        },
        "prev_hot": prev.hot_water if prev else ZERO,
        "prev_cold": prev.cold_water if prev else ZERO,
        "prev_elect": prev.electricity if prev else ZERO,
        "has_draft": draft is not None,
        "draft_hot": draft.hot_water if draft else None,
        "draft_cold": draft.cold_water if draft else None,
        "draft_elect": draft.electricity if draft else None,
    }