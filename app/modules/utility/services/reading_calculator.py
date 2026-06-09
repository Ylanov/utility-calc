"""Единая точка расчёта стоимости одного MeterReading.

Используется ДВУМЯ путями:
  - gsheets_sync.promote_auto_approved_rows — при auto-approve подачи из
    Google Sheets, чтобы сразу проставлять реальные суммы (а не 0!).
  - app.scripts.recalc_zero_gsheets_readings — для пересчёта исторически
    «забытых» reading'ов где cost_* = 0 и total_cost = 0.

Контракт: на входе reading и tariff (опционально prev_reading), на выходе
dict со всеми cost_* + total_209/205/cost. Caller сохраняет в БД.

История появления (may 2026): до этого helper'а promote сохранял
MeterReading с total_cost=0, не вызывая calculate_utilities. Жилец
видел «нулевую квитанцию» при реальной подаче — в админке flag
GSHEETS_AUTO + total = 0 ₽, в PDF все умножения тариф×объём = 0.00.
Деньги физически не начислялись.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.modules.utility.models import MeterReading, Room, Tariff, User
from app.modules.utility.services.calculations import (
    CalculationError,
    calculate_utilities,
)


ZERO = Decimal("0.00")


def _to_dec(value) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def compute_reading_breakdown(
    *,
    user: User,
    room: Room,
    tariff: Tariff,
    current_hot: Decimal,
    current_cold: Decimal,
    current_elect: Decimal,
    prev_reading: Optional[MeterReading] = None,
    heating_season_active: bool = True,
    hot_water_heating_active: bool = True,
) -> dict:
    """Считает breakdown стоимости для reading и возвращает все поля,
    которые caller должен записать в MeterReading.

    Аргументы:
      current_hot/cold/elect — текущие показания счётчиков (накопленные).
      prev_reading           — предыдущая утверждённая подача жильца в этой
                               комнате; None означает baseline (первая подача,
                               расход не известен — возвращаем все нули).
      heating_season_active, hot_water_heating_active — сезонные переключатели
                               (см. SystemSetting). Caller обычно читает их через
                               `_load_seasonal(db)` (async) или своим способом
                               для sync-контекстов (gsheets promote, recalc-скрипт).

    Возвращает dict со ключами:
      cost_hot_water, cost_cold_water, cost_sewage, cost_electricity,
      cost_maintenance, cost_social_rent, cost_waste, cost_fixed_part
      — компоненты для setattr на MeterReading;
      total_cost, total_209, total_205 — итоги для записи в БД;
      sanity_warning — необязательное предупреждение от calculate_utilities
                       (передавать вверх для UI/логов).

    Raises CalculationError — если тариф пустой (см. calculate_utilities).
    """
    # Baseline: первая подача жильца → потребление-зависимые статьи = 0
    # (счётчик может быть «накручен» за годы, считать дельту от 0 = деньги
    # в миллионы). НО area-based начисления (содержание, найм, ТКО,
    # отопление) — ПЛАТЯТСЯ ВСЕГДА, независимо от показаний.
    #
    # Раньше тут возвращался полный набор нулей — это съедало законные
    # area×tariff начисления (~5000-7000 ₽/мес на жильца) ВСЕМ кто имел
    # только AUTO_GENERATED 0/0/0 baseline. Инцидент: Резунов апр-2026,
    # 5804 ₽ area-based не начислилось → формально жилец «не должен»,
    # система тихо теряла доход.
    #
    # Фикс: вызываем calculate_utilities с volume_*=0. Тогда:
    #   cost_hot_water / cold_water / sewage / electricity = 0 (как и было);
    #   cost_maintenance / social_rent / waste / fixed_part = area × tariff
    #     (полноценное начисление).
    if prev_reading is None:
        try:
            baseline_costs = calculate_utilities(
                user=user, room=room, tariff=tariff,
                volume_hot=ZERO, volume_cold=ZERO,
                volume_sewage=ZERO, volume_electricity_share=ZERO,
                heating_season_active=heating_season_active,
                hot_water_heating_active=hot_water_heating_active,
            )
        except CalculationError:
            # На случай битого тарифа — fallback на старое поведение, чтобы
            # save-точка не падала. Лучше нулевой счёт чем 500-ка.
            baseline_costs = {
                "cost_hot_water": ZERO, "cost_cold_water": ZERO,
                "cost_sewage": ZERO, "cost_electricity": ZERO,
                "cost_maintenance": ZERO, "cost_social_rent": ZERO,
                "cost_waste": ZERO, "cost_fixed_part": ZERO,
                "total_cost": ZERO, "sanity_warning": None,
            }
        cost_205_b = baseline_costs.get("cost_social_rent", ZERO)
        cost_209_b = baseline_costs.get("total_cost", ZERO) - cost_205_b
        return {
            **baseline_costs,
            "total_209": cost_209_b,
            "total_205": cost_205_b,
            "is_baseline": True,
            "meter_decreased": False,
            "prev_is_auto": False,
        }

    p_hot = _to_dec(prev_reading.hot_water)
    p_cold = _to_dec(prev_reading.cold_water)
    p_elect = _to_dec(prev_reading.electricity)

    cur_hot = _to_dec(current_hot)
    cur_cold = _to_dec(current_cold)
    cur_elect = _to_dec(current_elect)

    # Дельты с защитой от отрицательных. Различаем 2 случая:
    #
    # 1) prev — manual-подача (или GSHEETS_IMPORT/AUTO/без флага). Если cur<prev,
    #    это «счётчик упал» — физически невозможно. meter_decreased=True,
    #    caller (gsheets promote) переводит в conflict.
    #
    # 2) prev — AUTO_AVG/AUTO_NORM_SANCTION/AUTO_NO_HISTORY (мы САМИ начислили
    #    по среднему, потому что жилец пропустил месяц). Если cur<prev — это
    #    значит «AUTO переоценил». Это НЕ баг данных, нужно принять и
    #    пересчитать (см. skip_recalc.py). meter_decreased=False, но возвращаем
    #    prev_is_auto=True — caller вызовет ретроактивный пересчёт.
    AUTO_FLAGS = (
        # ВКЛЮЧАЯ обычный AUTO_NORM (не только _SANCTION): ежемесячная авто-добивка
        # нормативом — это НАША оценка, а не реальное показание. Без него реальная
        # подача жильца ниже норматива блокировалась как «счётчик упал»
        # (Гюрджян 95<103, Теплоухов 830<835).
        "AUTO_NORM", "AUTO_AVG", "AUTO_NORM_SANCTION",
        "AUTO_AVG_FALLBACK", "AUTO_NO_HISTORY",
        "AUTO_GENERATED",  # legacy
    )
    prev_flags = (prev_reading.anomaly_flags or "").upper()
    prev_is_auto = any(f in prev_flags for f in AUTO_FLAGS)

    raw_decreased = (
        cur_hot < p_hot or cur_cold < p_cold or cur_elect < p_elect
    )
    # meter_decreased сигналим ТОЛЬКО если prev manual, чтобы автокоррекция
    # после возврата (см. услугу skip_recalc) не блокировалась.
    meter_decreased = raw_decreased and not prev_is_auto
    d_hot = max(ZERO, cur_hot - p_hot)
    d_cold = max(ZERO, cur_cold - p_cold)
    d_elect = max(ZERO, cur_elect - p_elect)

    # Доля жильца в комнатном расходе электричества (как в client_readings).
    residents = Decimal(user.residents_count or 1)
    total_room = Decimal(room.total_room_residents or 1)
    if total_room <= 0:
        total_room = Decimal("1")
    elect_share = (residents / total_room) * d_elect

    # Расчёт. Может бросить CalculationError если тариф полностью пуст —
    # пропагандируем наверх, caller решит что делать (логировать и пропустить).
    costs = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=d_hot, volume_cold=d_cold,
        volume_sewage=d_hot + d_cold,
        volume_electricity_share=elect_share,
        heating_season_active=heating_season_active,
        hot_water_heating_active=hot_water_heating_active,
    )

    # Декомпозиция total → 209/205. Та же логика что в client_readings.py:
    # 205 = социальный найм; 209 = всё остальное (коммуналка + содержание +
    # отопление + ТКО). У этого reading нет долгов/корректировок (gsheets
    # только что создал) — их учитывает client_readings/admin_approve когда
    # подача проходит через ручной flow.
    cost_rent = costs["cost_social_rent"]
    total_205 = cost_rent
    total_209 = costs["total_cost"] - cost_rent
    total_cost = costs["total_cost"]

    return {
        "cost_hot_water":   costs["cost_hot_water"],
        "cost_cold_water":  costs["cost_cold_water"],
        "cost_sewage":      costs["cost_sewage"],
        "cost_electricity": costs["cost_electricity"],
        "cost_maintenance": costs["cost_maintenance"],
        "cost_social_rent": costs["cost_social_rent"],
        "cost_waste":       costs["cost_waste"],
        "cost_fixed_part":  costs["cost_fixed_part"],
        "total_cost":       total_cost,
        "total_209":        total_209,
        "total_205":        total_205,
        "sanity_warning":   costs.get("sanity_warning"),
        "is_baseline":      False,
        # Сигнал «счётчик упал» — только когда prev manual (см. AUTO_FLAGS).
        # gsheets promote по этому флагу переводит в conflict для ручного разбора.
        "meter_decreased":  meter_decreased,
        # prev_is_auto=True значит prev был AUTO_AVG / AUTO_NORM_SANCTION и т.п.
        # Caller может запустить retroactive recalc (skip_recalc.py).
        "prev_is_auto":     prev_is_auto,
    }


def find_chronological_prev_reading_sync(db_session, *, user_id: int, room_id: int, before_period_id: int) -> Optional[MeterReading]:
    """Находит предыдущее (по биллинговому периоду) approved-reading жильца.

    КРИТИЧНО: ищем по period.id, а НЕ по MeterReading.created_at.

    Почему это важно (инцидент may 2026):
      Жилец подавал показания за разные месяцы в разное время через
      гугл-таблицу. В БД получалось:
        Апрель 2026: hot=151.5, created_at=2026-04-15 (импорт в апреле)
        Февраль 2026: hot=142.89, created_at=2026-05-10 (исторический
                       импорт админом в мае)
      Поиск prev по created_at давал для февральского reading'а
      «предыдущим» апрельский (который ХРОНОЛОГИЧЕСКИ позже!), и
      дельта получалась 142.89 - 151.5 = -8.6. safe_positive() обнулял,
      total_cost=0, в PDF «-8.60 м³ × 40.0000 = 0.00».

      Поиск по period_id строго отражает хронологию биллинга (при
      условии что period_id монотонно возрастает по времени, что
      обеспечивается check_auto_period_task: каждый новый месяц —
      новый период с +1 к id).

    Sync-версия для использования из Celery / scripts (sync_db_session).
    """
    return (
        db_session.query(MeterReading)
        .filter(
            MeterReading.user_id == user_id,
            MeterReading.room_id == room_id,
            MeterReading.is_approved.is_(True),
            MeterReading.period_id < before_period_id,
        )
        .order_by(MeterReading.period_id.desc())
        .first()
    )


# =====================================================================
# Какие prev-readings НЕ должны участвовать в подсчёте дельты следующего
# периода (инцидент may 2026): жилец имел AUTO_GENERATED (нулевые
# значения) или DATA_OVERFLOW_RESET (обнулено после аномалии), потом
# GSheets подал реальные 1 468 ГВС — дельта вычислилась как 1468-0 →
# счёт за подогрев 456 562 ₽. Эти reading'и НЕ репрезентативны для
# baseline: их hot/cold/elect не отражают физические показания счётчика.
# =====================================================================
PREV_SKIP_FLAGS = frozenset({
    "AUTO_GENERATED",      # initial setup / fill, значения 0
    "DATA_OVERFLOW_RESET", # обнулено cleanup_anomaly_readings
    "AUTO_NO_HISTORY",     # фоновое начисление при пропуске, значения 0
    # Bug E2-D (28.05.2026, инцидент с Вастаевым):
    # AUTO_AVG / AUTO_AVG_FALLBACK / AUTO_NORM_SANCTION — это значения,
    # которые насчитала САМА система (last_real + средняя дельта или
    # норматив). Когда жилец потом подаёт реальные показания за более
    # ранний месяц (Февраль/Март после уже созданного Апрельского
    # AUTO_AVG из-за прыгающих period_id), валидатор сравнивал новое
    # реальное (138) с синтетическим Апрельским AUTO_AVG (142) и валил
    # с «счётчик упал». Хотя физически жилец прав — это AUTO_AVG
    # переоценил. Теперь AUTO_AVG не считается meaningful prev, новый
    # reading проходит валидацию, а skip_recalc ретроактивно
    # пересчитывает Апрельский AUTO_AVG.
    "AUTO_NORM",           # обычная месячная авто-добивка нормативом — наша оценка,
                           # а не реальное показание. Иначе реальная подача ниже
                           # норматива валилась «счётчик упал» (Гюрджян/Теплоухов).
    "AUTO_AVG",
    "AUTO_AVG_FALLBACK",
    "AUTO_NORM_SANCTION",
    "MANUAL_RECEIPT",      # квитанция без показаний (только сальдо)
    "ONE_TIME_CHARGE_BASELINE",  # выселение с baseline-flag
    # Замена счётчика: METER_CLOSED — ФИНАЛЬНОЕ показание СТАРОГО прибора
    # (большое накопленное значение). Его НЕЛЬЗЯ брать как prev для нового
    # счётчика — иначе новая малая подача < старого → «счётчик упал»/блок.
    # Валидный prev после замены — METER_REPLACEMENT (новый baseline, малое
    # начальное значение) — он НЕ в skip.
    "METER_CLOSED",
    "STATIC_RENT",         # статичный наём дома (place_type=house) — не meter-событие
})


def is_meaningful_prev(reading: Optional[MeterReading]) -> bool:
    """True если reading годится как prev для расчёта delta.

    Reading НЕ годится когда его флаги говорят что hot/cold/elect — синтетические
    (заглушки, нули). Использование таких как baseline даёт фантастические
    суммы при следующей реальной подаче.

    BASELINE / GSHEETS_AUTO_BASELINE / INITIAL_SETUP оставлены как годные — у
    них реальные первичные значения счётчика.
    """
    if reading is None:
        return False
    flags = (reading.anomaly_flags or "").upper()
    for skip in PREV_SKIP_FLAGS:
        if skip in flags:
            return False
    return True


def find_meaningful_prev_reading_sync(
    db_session, *, user_id: int, room_id: int, before_period_id: int
) -> Optional[MeterReading]:
    """Как find_chronological_prev_reading_sync, но пропускает reading'и с
    PREV_SKIP_FLAGS (AUTO_GENERATED, DATA_OVERFLOW_RESET, и т.п.).

    Если в истории есть только такие — возвращает None (caller трактует как
    baseline, расход = 0).
    """
    rows = (
        db_session.query(MeterReading)
        .filter(
            MeterReading.user_id == user_id,
            MeterReading.room_id == room_id,
            MeterReading.is_approved.is_(True),
            MeterReading.period_id < before_period_id,
        )
        .order_by(MeterReading.period_id.desc())
        .limit(20)  # лимит на случай длинной цепочки заглушек
        .all()
    )
    for r in rows:
        if is_meaningful_prev(r):
            return r
    return None


__all__ = [
    "compute_reading_breakdown",
    "CalculationError",
    "find_chronological_prev_reading_sync",
    "find_meaningful_prev_reading_sync",
    "is_meaningful_prev",
    "PREV_SKIP_FLAGS",
]
