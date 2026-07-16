# Детали по жильцу: история подач квартиры, финансовая справка (expandable-
# панель), карточка «Жилец 360°», расчёт опубликованного баланса 209/205.
# Вербатим-перенос из admin_reports.py (строки 1359-2052), поведение 1:1.

from typing import Optional

from fastapi import Depends, HTTPException, Query
from sqlalchemy import desc, text, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.modules.utility.models import (
    User, MeterReading, BillingPeriod, Adjustment, Room,
    DebtImportLog, GSheetsImportRow,
)
from app.modules.utility.services.gsheets_sync import normalize_fio
from app.modules.utility.services.period_helpers import period_chron_key

from ._shared import logger, router


# =========================================================================
# ДЕТАЛИ ПО ОДНОМУ ЖИЛЬЦУ (expandable-панель в «Финансовой отчётности»)
# =========================================================================
# Возвращает всё, что нужно показать в развёрнутой строке:
#   * основная квитанция за выбранный период (детализация по статьям, дельты)
#   * история показаний за 6 последних периодов (счётчики + дельты + источник)
#   * корректировки (Adjustment) за эти периоды с пояснениями
#   * активный договор найма (№/дата) — чтобы не лезть в другую вкладку
# Один запрос вместо N+1 — подтягиваем всё батчами.

def _infer_source_flag(anomaly_flags):
    """Bug AN: расширено различение auto-стратегий вместо единого «auto».
    UI показывает админу, *как именно* система начислила: норматив×коэф,
    среднее по дельтам, повтор предыдущего, или baseline."""
    if not anomaly_flags:
        return "manual"
    af = anomaly_flags.upper()
    if "GSHEETS" in af:
        return "gsheets"
    if "ONE_TIME_CHARGE" in af:
        return "one_time"
    if "AUTO_NORM_SANCTION" in af:
        return "auto_norm_sanction"
    if "AUTO_AVG_FALLBACK" in af:
        return "auto_avg_fallback"
    if "AUTO_AVG" in af:
        return "auto_avg"
    if "AUTO_NO_HISTORY" in af:
        return "auto_no_history"
    if "AUTO_GENERATED" in af:
        return "auto"
    if "MANUAL_RECEIPT" in af:
        return "manual_receipt"
    if "BASELINE" in af:
        return "baseline"
    if "INITIAL_SETUP" in af:
        return "initial"
    if "METER_CLOSED" in af or "METER_REPLACEMENT" in af:
        return "meter_op"
    return "app"


@router.get("/api/admin/rooms/{room_id}/submission-history")
async def get_room_submission_history(
    room_id: int,
    periods: int = Query(12, ge=1, le=24,
                         description="Сколько последних периодов вернуть (1-24)"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """История подач по КВАРТИРЕ: по каждому месяцу (периоду) — какие показания
    подали жильцы этой комнаты (ФИО, тип, ГВС/ХВС/электр, сумма, источник).
    Привязка по `MeterReading.room_id` — ловит и текущих, и прошлых жильцов
    помещения. Для разворота квартиры в «Финансовой отчётности» (анализ квартиры
    по месяцам подач). Периоды — в хронологическом порядке (свежие сверху)."""
    if current_user.role not in ("accountant", "admin", "financier"):
        raise HTTPException(403, "Доступ запрещён")

    room = await db.get(Room, room_id)

    rows = (await db.execute(
        select(MeterReading, User, BillingPeriod)
        .join(BillingPeriod, BillingPeriod.id == MeterReading.period_id)
        .outerjoin(User, User.id == MeterReading.user_id)
        .where(
            MeterReading.room_id == room_id,
            MeterReading.is_approved.is_(True),
        )
    )).all()

    by_period: dict[int, dict] = {}
    for reading, user, period in rows:
        slot = by_period.get(period.id)
        if slot is None:
            slot = by_period[period.id] = {
                "period_id": period.id,
                "period_name": period.name,
                "_chrono": period_chron_key(period.name),
                "submissions": [],
                "total_cost": 0.0,
                "debt": 0.0,
                "overpayment": 0.0,
            }
        debt = float((reading.debt_209 or 0) + (reading.debt_205 or 0))
        overpay = float((reading.overpayment_209 or 0) + (reading.overpayment_205 or 0))
        slot["submissions"].append({
            "user_id": user.id if user else None,
            "username": user.username if user else "—",
            "full_name": getattr(user, "full_name", None) if user else None,
            "resident_type": (getattr(user, "resident_type", None) or "family") if user else None,
            "reading_id": reading.id,
            "is_approved": bool(reading.is_approved),
            "hot_water": float(reading.hot_water or 0),
            "cold_water": float(reading.cold_water or 0),
            "electricity": float(reading.electricity or 0),
            "total_cost": float(reading.total_cost or 0),
            "total_209": float(reading.total_209 or 0),
            "total_205": float(reading.total_205 or 0),
            "debt": debt,
            "overpayment": overpay,
            "source": _infer_source_flag(reading.anomaly_flags),
            "created_at": reading.created_at.isoformat() if reading.created_at else None,
        })
        slot["total_cost"] += float(reading.total_cost or 0)
        slot["debt"] += debt
        slot["overpayment"] += overpay

    history = sorted(by_period.values(), key=lambda s: s["_chrono"], reverse=True)[:periods]
    for s in history:
        s.pop("_chrono", None)
        s["submissions"].sort(key=lambda x: (x["username"] or ""))

    return {
        "room_id": room_id,
        "address": (room.format_address if room else None) or (room.room_number if room else "—"),
        "dormitory_name": room.dormitory_name if room else None,
        "periods_count": len(history),
        "history": history,
    }


@router.get("/api/admin/residents/{user_id}/finance-detail")
async def get_resident_finance_detail(
    user_id: int,
    period_id: Optional[int] = Query(None, description="Период квитанции. По умолчанию — последний."),
    history_periods: int = Query(6, ge=1, le=24),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Детальная финансовая справка по жильцу для разворачивающейся панели.

    Используется в «Финансовой отчётности» — админ кликает на строку жильца
    и получает всю финансовую картину: текущая квитанция + история счётчиков
    за 6 мес + корректировки + договор.
    """
    if current_user.role not in ("accountant", "admin", "financier"):
        raise HTTPException(403, "Доступ запрещён")

    user = (await db.execute(
        select(User)
        .options(selectinload(User.room))
        .where(User.id == user_id, User.is_deleted.is_(False))
    )).scalars().first()
    if not user:
        raise HTTPException(404, "Жилец не найден")

    # 1) Определяем целевой период.
    if period_id:
        period = await db.get(BillingPeriod, period_id)
    else:
        period = (await db.execute(
            select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(1)
        )).scalars().first()
    if not period:
        raise HTTPException(400, "Периоды не заведены — нет данных для показа")

    # 2) N последних периодов (включая текущий) для истории — БИЛЛИНГОВО.
    # Раньше сортировали по `BillingPeriod.id.desc()`, но id отражает порядок
    # создания записи в БД, а не биллинговый месяц. Если админ задним числом
    # импортировал «Февраль 2026» в мае, у него period.id > чем у мая, и в
    # таблице февраль появлялся ВЫШЕ майских данных. Из-за этого дельты тоже
    # съезжали (prev определялось по created_at, см. _prev_for ниже).
    # Теперь сортируем по распарсенному `(year, month)` имени периода —
    # хронологически. Нестандартные имена («Начальный период», тестовые)
    # получают ключ (0, 0) и оказываются в самом начале (baseline).
    all_periods = (await db.execute(select(BillingPeriod))).scalars().all()
    cur_key = period_chron_key(period.name)
    # Только периоды у которых биллинговая хронология <= текущей.
    # Это аналог прежнего `BillingPeriod.id <= period.id`, но корректный.
    periods_filtered = [p for p in all_periods if period_chron_key(p.name) <= cur_key]
    periods = sorted(
        periods_filtered,
        key=lambda p: period_chron_key(p.name),
        reverse=True,
    )[:history_periods]
    period_ids = [p.id for p in periods]
    period_name_map = {p.id: p.name for p in periods}

    # 3) Одним запросом — все показания жильца за эти периоды (включая drafts).
    readings = (await db.execute(
        select(MeterReading)
        .where(
            MeterReading.user_id == user_id,
            MeterReading.period_id.in_(period_ids) if period_ids else False,
        )
    )).scalars().all()

    # Для дельт нужно предыдущее approved показание ЖИЛЬЦА В ЭТОЙ КОМНАТЕ
    # в БИЛЛИНГОВОЙ хронологии (не по created_at — задний-числом импорт ломает).
    # Загружаем ВСЕ его approved readings (а не только за выбранные history N),
    # потому что предыдущее показание для самого раннего из N может лежать
    # ВНЕ выборки. Затем строим словарь reading.id → prev_reading.
    prev_reading_map: dict[int, Optional[MeterReading]] = {}
    if user.room_id:
        all_user_readings = (await db.execute(
            select(MeterReading, BillingPeriod)
            .join(BillingPeriod, BillingPeriod.id == MeterReading.period_id)
            .where(
                MeterReading.user_id == user_id,
                MeterReading.room_id == user.room_id,
                MeterReading.is_approved.is_(True),
            )
        )).all()
        # Сортируем хронологически ASC: baseline → старые → новые.
        all_user_readings_chronological = sorted(
            all_user_readings,
            key=lambda row: period_chron_key(row[1].name),
        )
        # Двигаемся по цепочке: текущему reading prev = последний из СТРОГО
        # БОЛЕЕ РАННЕГО периода. Дубль в том же периоде (debt-черновик 1С,
        # повторная подача) prev'ом быть не должен — иначе дельта считается
        # против записи с теми же цифрами и показывает 0 (инцидент Мороз).
        prev_reading: Optional[MeterReading] = None
        last_key = None
        prev_of_key: Optional[MeterReading] = None  # prev для текущей группы периода
        for r, _bp in all_user_readings_chronological:
            k = period_chron_key(_bp.name)
            if k != last_key:
                prev_of_key = prev_reading
                last_key = k
            prev_reading_map[r.id] = prev_of_key
            prev_reading = r

    def _prev_for(reading):
        """Предыдущее approved показание жильца в биллинговой хронологии
        (а не по `created_at` — задний-числом импорт ломал предыдущую логику,
        см. инцидент мая 2026 с Сорокиным С.А.)."""
        return prev_reading_map.get(reading.id)

    # 4) Корректировки за эти периоды.
    adjustments = []
    if period_ids:
        adj_rows = (await db.execute(
            select(Adjustment)
            .where(Adjustment.user_id == user_id, Adjustment.period_id.in_(period_ids))
            .order_by(Adjustment.period_id.desc(), Adjustment.created_at.desc())
        )).scalars().all()
        for a in adj_rows:
            adjustments.append({
                "id": a.id,
                "period_id": a.period_id,
                "period_name": period_name_map.get(a.period_id),
                "amount": float(a.amount or 0),
                "description": a.description or "",
                "account_type": a.account_type or "209",
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })

    # 5) Договор найма — активный.
    from app.modules.utility.models import RentalContract
    contract_row = (await db.execute(
        select(RentalContract)
        .where(
            RentalContract.user_id == user_id,
            RentalContract.is_active.is_(True),
        )
        .limit(1)
    )).scalars().first()
    contract_data = None
    if contract_row:
        contract_data = {
            "id": contract_row.id,
            "number": contract_row.number,
            "signed_date": contract_row.signed_date.isoformat() if contract_row.signed_date else None,
            "valid_until": contract_row.valid_until.isoformat() if contract_row.valid_until else None,
            "has_file": bool(contract_row.file_s3_key),
            "file_name": contract_row.file_name,
        }

    # 6) Сборка истории показаний (6 периодов).
    readings_by_period = {r.period_id: r for r in readings}
    history = []
    for p in periods:
        r = readings_by_period.get(p.id)
        if r is None:
            history.append({
                "period_id": p.id,
                "period_name": p.name,
                "reading_id": None,
                "is_approved": False,
                "has_pdf": False,
                "source": None,
                "flags": [],
                "hot_water": None, "cold_water": None, "electricity": None,
                "delta_hot": None, "delta_cold": None, "delta_elect": None,
                "total_cost": None, "total_209": None, "total_205": None,
            })
            continue
        prev = _prev_for(r)
        p_hot = float(prev.hot_water or 0) if prev else 0.0
        p_cold = float(prev.cold_water or 0) if prev else 0.0
        p_elect = float(prev.electricity or 0) if prev else 0.0
        cur_hot = float(r.hot_water or 0)
        cur_cold = float(r.cold_water or 0)
        cur_elect = float(r.electricity or 0)
        flags_list = [f.strip() for f in (r.anomaly_flags or "").split(",") if f.strip()]
        history.append({
            "period_id": p.id,
            "period_name": p.name,
            "reading_id": r.id,
            "is_approved": bool(r.is_approved),
            "has_pdf": bool(getattr(r, "pdf_s3_key", None)),
            "source": _infer_source_flag(r.anomaly_flags),
            "flags": flags_list,
            "hot_water": cur_hot,
            "cold_water": cur_cold,
            "electricity": cur_elect,
            "delta_hot": cur_hot - p_hot if prev else None,
            "delta_cold": cur_cold - p_cold if prev else None,
            "delta_elect": cur_elect - p_elect if prev else None,
            "total_cost": float(r.total_cost or 0),
            "total_209": float(r.total_209 or 0),
            "total_205": float(r.total_205 or 0),
        })

    # 7) Детализация текущей квитанции.
    cur = readings_by_period.get(period.id)
    current = None
    if cur:
        prev = _prev_for(cur)
        current = {
            "reading_id": cur.id,
            "is_approved": bool(cur.is_approved),
            "source": _infer_source_flag(cur.anomaly_flags),
            "anomaly_flags": cur.anomaly_flags,
            "anomaly_score": int(cur.anomaly_score or 0),
            "hot_water": float(cur.hot_water or 0),
            "cold_water": float(cur.cold_water or 0),
            "electricity": float(cur.electricity or 0),
            "prev_hot": float(prev.hot_water or 0) if prev else 0.0,
            "prev_cold": float(prev.cold_water or 0) if prev else 0.0,
            "prev_elect": float(prev.electricity or 0) if prev else 0.0,
            "delta_hot": float((cur.hot_water or 0) - (prev.hot_water or 0)) if prev else None,
            "delta_cold": float((cur.cold_water or 0) - (prev.cold_water or 0)) if prev else None,
            "delta_elect": float((cur.electricity or 0) - (prev.electricity or 0)) if prev else None,
            "total_cost": float(cur.total_cost or 0),
            "total_209": float(cur.total_209 or 0),
            "total_205": float(cur.total_205 or 0),
            "debt_209": float(cur.debt_209 or 0),
            "debt_205": float(cur.debt_205 or 0),
            "overpayment_209": float(cur.overpayment_209 or 0),
            "overpayment_205": float(cur.overpayment_205 or 0),
            "hot_correction": float(cur.hot_correction or 0),
            "cold_correction": float(cur.cold_correction or 0),
            "electricity_correction": float(cur.electricity_correction or 0),
            "sewage_correction": float(cur.sewage_correction or 0),
            "costs": {
                "cost_hot_water": float(cur.cost_hot_water or 0),
                "cost_cold_water": float(cur.cost_cold_water or 0),
                "cost_sewage": float(cur.cost_sewage or 0),
                "cost_electricity": float(cur.cost_electricity or 0),
                "cost_maintenance": float(cur.cost_maintenance or 0),
                "cost_social_rent": float(cur.cost_social_rent or 0),
                "cost_waste": float(cur.cost_waste or 0),
                "cost_fixed_part": float(cur.cost_fixed_part or 0),
            },
            "has_pdf": bool(getattr(cur, "pdf_s3_key", None)),
        }

    return {
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
            "residents_count": (user.room.total_room_residents if user.room and user.room.total_room_residents else 1),
            "resident_type": getattr(user, "resident_type", "family"),
            "billing_mode": getattr(user, "billing_mode", "by_meter"),
            "room": (
                {
                    "id": user.room.id,
                    "dormitory_name": user.room.dormitory_name,
                    "room_number": user.room.room_number,
                    "apartment_area": float(user.room.apartment_area or 0),
                    "total_room_residents": user.room.total_room_residents or 1,
                    "hw_meter_serial": getattr(user.room, "hw_meter_serial", None),
                    "cw_meter_serial": getattr(user.room, "cw_meter_serial", None),
                    "el_meter_serial": getattr(user.room, "el_meter_serial", None),
                }
                if user.room else None
            ),
        },
        "period": {"id": period.id, "name": period.name},
        "current": current,
        "history": history,  # от свежих к старым
        "adjustments": adjustments,
        "contract": contract_data,
        "balance": await _compute_user_balance(db, user.id),
    }


# ─── Жилец 360°: единый поиск/карточка по ФИО (read-only агрегатор) ──────────
_GIS_FINDINGS_KEY = "gisgmp_findings"
_GIS_SOURCE_LABEL = "ГИС ГМП (авто)"


def _p360_reading_source(flags):
    """Источник боевого показания по anomaly_flags (как в едином реестре)."""
    f = (flags or "").upper()
    if "GSHEETS" in f:
        return "gsheets", "📄 Google Sheets"
    if "MANUAL_RECEIPT" in f:
        return "manual", "✍️ Вручную"
    if any(a in f for a in ("AUTO_NORM", "AUTO_AVG", "AUTO_GENERATED",
                            "AUTO_NO_HISTORY", "STATIC_RENT")):
        return "auto", "🤖 Норматив/авто"
    return "user", "📱 QR/приложение"


@router.get("/api/admin/residents/{user_id}/passport-360",
            summary="Карточка жильца 360° — всё из всех источников")
async def get_resident_passport_360(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Единая карточка: где живёт, тариф (за что платит), ВСЯ история показаний
    (боевые+черновики+буфер Google-таблицы+QR соседей по комнате) и долги/
    переплаты 1С (последний импорт/черновик, вкл. неутверждённые) + ГИС ГМП.
    Read-only, композиция готовых источников — ничего не пишет."""
    if current_user.role not in ("accountant", "admin", "financier"):
        raise HTTPException(403, "Доступ запрещён")

    # БЕЗ фильтра is_deleted — карточка открывает и выселенных/должников без
    # комнаты (ключ — user_id, не ФИО).
    # Грузим room И room.tariff заранее — иначе ленивый room.tariff в async даст
    # MissingGreenlet (sync-IO в асинхронном контексте).
    user = (await db.execute(
        select(User)
        .options(selectinload(User.room).selectinload(Room.tariff))
        .where(User.id == user_id)
    )).scalars().first()
    if not user:
        raise HTTPException(404, "Жилец не найден")
    room = user.room

    # 1) Опубликованный баланс (что жилец видит) — напрямую через _compute_user_balance
    # (берёт 209/205 с РАЗНЫХ последних показаний; работает и для выселенных).
    published_balance = await _compute_user_balance(db, user.id)

    # 1b) Начисления ПО ТАРИФУ «один в один как в финотчётности» — переиспользуем
    # тот же finance-detail (история периодов с total_209/205/итого + детальная
    # квитанция с разбивкой по услугам). У выселенных 404 → None (деградируем).
    finance = None
    try:
        finance = await get_resident_finance_detail(
            user_id, period_id=None, history_periods=12,
            current_user=current_user, db=db)
    except HTTPException:
        finance = None

    # 2) Тариф — эффективный = тариф КОМНАТЫ (что-биллится).
    tariff = room.tariff if room else None
    tariff_info = None
    if tariff:
        tariff_info = {
            "id": tariff.id, "name": tariff.name,
            "tariff_type": getattr(tariff, "tariff_type", None),
            "per_capita_amount": float(getattr(tariff, "per_capita_amount", 0) or 0),
            "norm_per_capita": {
                "hot": float(getattr(tariff, "hw_norm_per_capita", 0) or 0),
                "cold": float(getattr(tariff, "cw_norm_per_capita", 0) or 0),
                "elect": float(getattr(tariff, "el_norm_per_capita", 0) or 0),
            },
            "charges": {fld: bool(getattr(tariff, fld, False)) for fld in (
                "charge_hot_water", "charge_cold_water", "charge_sewage",
                "charge_electricity", "charge_heating", "charge_maintenance",
                "charge_social_rent", "charge_waste")
                if hasattr(tariff, fld)},
        }

    # 3) ВСЯ история показаний из всех путей.
    pname = {p.id: p.name for p in
             (await db.execute(select(BillingPeriod))).scalars().all()}
    # 3a) боевые MeterReading по user_id ИЛИ комнате (QR/подача соседа привязаны
    # к комнате), включая черновики (is_approved=False).
    rd_cond = MeterReading.user_id == user_id
    if user.room_id:
        rd_cond = (MeterReading.user_id == user_id) | (MeterReading.room_id == user.room_id)
    mrs = (await db.execute(
        select(MeterReading).where(rd_cond).order_by(MeterReading.created_at.desc())
    )).scalars().all()
    readings = []
    for r in mrs:
        skey, slabel = _p360_reading_source(getattr(r, "anomaly_flags", None))
        readings.append({
            "row_type": "reading", "id": r.id,
            "period": pname.get(r.period_id, "—"), "period_id": r.period_id,
            "date": r.created_at.isoformat() if r.created_at else None,
            "hot_water": float(r.hot_water or 0), "cold_water": float(r.cold_water or 0),
            "electricity": float(r.electricity or 0),
            "is_approved": bool(r.is_approved),
            "own": r.user_id == user_id,   # False = подал сосед/представитель по комнате
            "source": skey, "source_label": slabel,
            "total_209": float(getattr(r, "total_209", 0) or 0),
            "total_205": float(getattr(r, "total_205", 0) or 0),
        })
    # 3b) ВСЕ подачи из Google-таблицы по жильцу, за все даты, ВКЛЮЧАЯ уже
    # промоутнутые (reading_id != NULL). Иначе терялись: (1) непривязанные матчером
    # (matched_user_id NULL) и (2) промоутнутые — когда НЕСКОЛЬКО месячных подач
    # слиплись в ОДНО показание (reading_id одинаковый) → в карточке видно меньше
    # подач, чем в таблице. Ищем и по matched_user_id, и по самому ФИО (raw_fio).
    target_fio = normalize_fio(user.username or "")
    surname = (user.username or "").split()[0] if user.username else ""
    gq = select(GSheetsImportRow)
    if surname:
        gq = gq.where(or_(GSheetsImportRow.matched_user_id == user_id,
                          GSheetsImportRow.raw_fio.ilike(f"%{surname}%")))
    else:
        gq = gq.where(GSheetsImportRow.matched_user_id == user_id)
    gsr_all = (await db.execute(
        gq.order_by(GSheetsImportRow.sheet_timestamp.desc().nullslast())
    )).scalars().all()
    # ilike по фамилии — широкий; оставляем точное совпадение по нормализованному
    # ФИО либо уже привязанные к этому user_id.
    gsr = [g for g in gsr_all
           if g.matched_user_id == user_id or normalize_fio(g.raw_fio or "") == target_fio]
    buffer_rows = [{
        "row_type": "buffer", "id": g.id,
        "date": (g.sheet_timestamp or g.created_at).isoformat()
                if (g.sheet_timestamp or g.created_at) else None,
        "hot_water": float(g.hot_water or 0), "cold_water": float(g.cold_water or 0),
        "status": g.status, "match_score": g.match_score,
        "raw_room": g.raw_room_number, "raw_fio": g.raw_fio,
        "linked": g.matched_user_id == user_id,   # False = найдено по ФИО, не привязано
        "promoted": g.reading_id is not None,      # True = уже стало показанием
        "reading_id": g.reading_id,
        "source": "gsheets", "source_label": "📄 Google Sheets",
    } for g in gsr]

    # 4) Долги/переплаты — 1С (последний импорт/черновик по 209/205, вкл.
    # неутверждённые staged) + ГИС ГМП.
    onec = {}
    for acc in ("209", "205"):
        log = (await db.execute(
            select(DebtImportLog).where(
                DebtImportLog.account_type == acc,
                DebtImportLog.status.in_(["staged", "completed"]),
                DebtImportLog.file_name != _GIS_SOURCE_LABEL,
            ).order_by(desc(DebtImportLog.started_at)).limit(1)
        )).scalars().first()
        e = {"debt": 0.0, "overpayment": 0.0, "file": None,
             "status": None, "at": None, "found": False}
        if log:
            e.update(file=log.file_name, status=log.status,
                     at=log.started_at.isoformat() if log.started_at else None)
            st = (log.applied_state or {}).get(str(user_id))
            if not st:
                for v in (log.applied_state or {}).values():
                    if (v.get("username") or "") == user.username:
                        st = v
                        break
            if st:
                e.update(debt=float(st.get(f"debt_{acc}") or 0),
                         overpayment=float(st.get(f"overpayment_{acc}") or 0), found=True)
            else:
                # Запасной матч по ФИО среди «не найденных» — ТОЛЬКО если совпадение
                # единственное (иначе риск приписать чужой долг при коллизии ФИО).
                tgt = normalize_fio(user.username)
                hits = [nf for nf in (log.not_found_users or [])
                        if normalize_fio(nf.get("fio") or "") == tgt]
                if len(hits) == 1:
                    e.update(debt=float(hits[0].get("debt") or 0),
                             overpayment=float(hits[0].get("overpayment") or 0), found=True)
                elif len(hits) > 1:
                    e["ambiguous"] = True
        onec[acc] = e

    # ГИС ГМП — долг из находок. Сначала по АВТОРИТЕТНОМУ matched_user_id (релей
    # его проставляет), и только если такого нет — запасной матч по ФИО (с риском
    # коллизии однофамильцев, поэтому второй приоритет).
    gis = {"debt_209": 0.0, "debt_205": 0.0, "found": False, "synced_at": None}
    try:
        fres = (await db.execute(
            text("SELECT (value::jsonb -> 'summary')::text AS s, "
                 "value::jsonb ->> 'synced_at' AS at FROM system_settings WHERE key=:k"),
            {"k": _GIS_FINDINGS_KEY},
        )).first()
        if fres and fres.s:
            import json as _json
            gis["synced_at"] = fres.at
            target = normalize_fio(user.username)
            rows_gis = _json.loads(fres.s)
            frow = next((x for x in rows_gis if x.get("matched_user_id") == user_id), None)
            if frow is None:
                frow = next((x for x in rows_gis
                             if normalize_fio(x.get("fio") or "") == target), None)
            if frow is not None:
                gis.update(debt_209=float(frow.get("debt_209") or 0),
                           debt_205=float(frow.get("debt_205") or 0), found=True)
    except Exception:
        logger.exception("[passport-360] ГИС находки не прочитаны")

    return {
        "resident": {
            "user_id": user.id, "fio": user.username,
            "login": getattr(user, "login", None), "full_name": user.full_name,
            "is_deleted": bool(user.is_deleted), "role": user.role,
            "room": ({
                "id": room.id, "address": room.format_address,
                "place_type": room.place_type, "dormitory_name": room.dormitory_name,
                "room_number": room.room_number,
                "is_singles_apartment": bool(getattr(room, "is_singles_apartment", False)),
                "total_room_residents": room.total_room_residents or 1,
            } if room else None),
            "tariff": tariff_info,
        },
        "readings": readings,    # боевые (свои + по комнате), вкл. черновики
        "buffer": buffer_rows,   # буфер Google-таблицы (не промоутнут)
        "finance": finance,      # расчёт по тарифу как в финотчётности (history+receipt)
        "debts": {
            "published": published_balance,  # баланс из MeterReading (опубликовано)
            "onec": onec,        # последний импорт/черновик 1С по 209/205
            "gis": gis,          # ГИС ГМП находки
        },
    }


async def _compute_user_balance(db: AsyncSession, user_id: int) -> dict:
    """Текущий баланс жильца — единое число «должен/переплатил» с учётом
    ОБОИХ счетов (209 коммуналка + 205 найм).

    debt/overpayment живут в каждом MeterReading периода. Импорт 1С
    обновляет их в АКТИВНОМ периоде, но если 209-импорт прошёл в Мае,
    а 205-импорт в Январе — самые свежие сальдо лежат на РАЗНЫХ
    reading-ах. Раньше брали один свежий с любым сальдо → теряли
    второй счёт (Галко: 209-долг = 26420.92 виден, 205-долг = 3125.50
    из старого reading «пропадал»).

    Фикс: отдельный поиск САМОГО СВЕЖЕГО reading где ненулевой 209,
    и отдельный — где ненулевой 205. balance_209/205 берутся независимо.

    КРИТИЧЕСКИЙ ФИКС (аудит финотчётности 2026-07-16): фильтр — по
    user_id (долг принадлежит ЛИЦЕВОМУ СЧЁТУ, models.py), НЕ по room_id.
    Раньше фильтровали по комнате: в комнате из N жильцов каждому
    показывался долг СОСЕДА со свежайшего reading'а, а QR-кошелёк
    (public_portal суммирует балансы жильцов комнаты) умножал один долг
    на N. Комната для баланса не нужна вовсе — долг следует за жильцом
    (в т.ч. отвязанным от комнаты).

    balance_X > 0  → жилец должен по этому счёту
    balance_X < 0  → переплата по этому счёту
    balance_X == 0 → ноль
    """
    # Свежий reading ЖИЛЬЦА с НЕНУЛЕВЫМ 209-сальдо
    latest_209 = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user_id,
            (MeterReading.debt_209 > 0) | (MeterReading.overpayment_209 > 0),
        ).order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    # Свежий reading ЖИЛЬЦА с НЕНУЛЕВЫМ 205-сальдо (может быть тот же или другой)
    latest_205 = (await db.execute(
        select(MeterReading).where(
            MeterReading.user_id == user_id,
            (MeterReading.debt_205 > 0) | (MeterReading.overpayment_205 > 0),
        ).order_by(MeterReading.created_at.desc()).limit(1)
    )).scalars().first()

    if not latest_209 and not latest_205:
        return {
            "balance_209": 0.0, "balance_205": 0.0, "total": 0.0,
            "kind": "zero", "source_209_reading_id": None,
            "source_205_reading_id": None,
        }

    debt_209 = float(latest_209.debt_209 or 0) if latest_209 else 0.0
    overpay_209 = float(latest_209.overpayment_209 or 0) if latest_209 else 0.0
    debt_205 = float(latest_205.debt_205 or 0) if latest_205 else 0.0
    overpay_205 = float(latest_205.overpayment_205 or 0) if latest_205 else 0.0

    balance_209 = debt_209 - overpay_209
    balance_205 = debt_205 - overpay_205
    total = balance_209 + balance_205

    if total > 0:
        kind = "debtor"
    elif total < 0:
        kind = "overpaid"
    else:
        kind = "even"

    return {
        "balance_209": round(balance_209, 2),
        "balance_205": round(balance_205, 2),
        "total": round(total, 2),
        "kind": kind,
        "debt_209": debt_209,
        "overpayment_209": overpay_209,
        "debt_205": debt_205,
        "overpayment_205": overpay_205,
        "source_209_reading_id": latest_209.id if latest_209 else None,
        "source_205_reading_id": latest_205.id if latest_205 else None,
    }
