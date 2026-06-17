"""skip_recalc.py — ретроактивный пересчёт AUTO-месяцев при возврате жильца.

Сценарий (типовой запрос мая 2026):
  Февраль: жилец подал hot=100, cold=200 — manual reading.
  Март:    жилец не подал — система начислила AUTO_AVG (например +3/+5 от среднего).
           Reading: hot=103, cold=205, total_cost ≈ 1500 ₽.
  Апрель:  жилец подал hot=108, cold=220 — реальная подача.

Без коррекции:
  Дельта от auto-март: hot=108-103=5, cold=220-205=15.
  Это считается за апрель → cost(апрель) = 5×t_water + 15×t_sewage + ...
  Сумма cost(март-auto) + cost(апрель) ≈ корректна **в total**, но:
    1) Помесячно «лесенка»: март занижен, апрель завышен (или наоборот).
    2) Если фактический апрель cur < AUTO-март, наш meter_decreased
       детектор раньше блокировал подачу как «счётчик упал». Это false-positive.

С коррекцией (этот модуль):
  Когда поступает factual reading после AUTO-цепочки:
    1) Найти last_manual (последний reading без AUTO-флага).
    2) Найти все AUTO-readings между ним и текущим.
    3) Реальное использование = cur - last_manual.
    4) Разделить равномерно по N+1 (N auto + 1 текущий) месяцам.
    5) Перезаписать hot_water/cold_water/electricity у каждого AUTO-reading
       так, чтобы помесячная дельта была одинаковой.
    6) Пересчитать cost_* у каждого AUTO-reading с актуальным тарифом.
    7) Audit-лог на каждую коррекцию + флаг AUTO_AVG_RECALCED_<DATE>.

Финансовый эффект:
  Sum(всех cost) до и после коррекции — может слегка отличаться (round-up
  при делении), но в пределах копеек. Главное: помесячно теперь честно.

Использование: вызывать из caller'а ПОСЛЕ того как сохранён текущий reading
(чтобы в auto-цепочке появилась «правая граница» — current_reading).
Helper async, использует ту же сессию что и caller. Транзакционность —
ответственность caller'а (мы делаем flush, не commit).
"""
from __future__ import annotations

import logging
from datetime import date as _date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.utility.models import (
    MeterReading, Tariff, User, Room, BillingPeriod,
)
from app.modules.utility.services.calculations import (
    calculate_utilities, costs_for_model_fields, D,
)
from app.modules.utility.services.period_helpers import period_chron_key

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")

# Эти флаги означают «начислено системой, не подал жилец». Подлежат
# ретроактивному пересчёту при возврате жильца к factual-подаче.
# AUTO_NORM/AUTO_NORM_SANCTION — текущие (NORM-only, 28.05.2026).
# Остальные — legacy от старой логики (AUTO_AVG до 28.05.2026).
# Все распознаются как auto чтобы skip_recalc корректно работал на
# старых reading'ах, которые ещё не пересчитаны.
_AUTO_FLAGS = (
    "AUTO_NORM",            # текущий: норматив × residents × 1
    "AUTO_NORM_SANCTION",   # текущий: норматив × residents × коэф
    "AUTO_AVG",             # legacy (до 28.05.2026): среднее по подачам
    "AUTO_AVG_FALLBACK",    # legacy: повтор последних или растущий норматив
    "AUTO_NO_HISTORY",      # legacy: только фикс-часть
    "AUTO_GENERATED",       # legacy: ранние авто-генерации
)


def _is_auto(reading: MeterReading) -> bool:
    flags = (reading.anomaly_flags or "").upper()
    return any(f in flags for f in _AUTO_FLAGS)


def _is_manual(reading: MeterReading) -> bool:
    """Manual = реальная подача жильца / админ-ввод / gsheets-import.
    Reading с пустым flag тоже считается manual."""
    if not reading.is_approved:
        return False
    if _is_auto(reading):
        return False
    # BASELINE'ы НЕ считаем manual (там нет реальной дельты от чего считать)
    flags = (reading.anomaly_flags or "").upper()
    if "BASELINE" in flags:
        return False
    return True


async def recalc_skip_chain(
    *,
    db: AsyncSession,
    user: User,
    room: Room,
    current_reading: MeterReading,
    tariff: Tariff,
    heating_season_active: bool = True,
    hot_water_heating_active: bool = True,
) -> Optional[dict]:
    """Главная функция: пересчитать AUTO-цепочку перед current_reading.

    Параметры:
      current_reading — TOLьko-что сохранённый factual reading (его hot_water /
                        cold_water / electricity уже актуальные, period_id уже
                        выставлен).
      tariff          — эффективный тариф (Room → User → default).
      heating_*, hw_* — те же сезонные флаги что используются в calculate_utilities
                        (см. _load_seasonal в settings.py + tariff.is_*_active_now).

    Возвращает dict со статистикой или None если коррекция не нужна:
      {
        "auto_readings_recalced": int,
        "last_manual_period_id": int,
        "real_delta_hot": Decimal,
        "real_delta_cold": Decimal,
        "real_delta_elect": Decimal,
        "applied": True,
      }
    """
    # 1. Тянем всю историю в этой паре (user_id, room_id), отсортированно
    #    по БИЛЛИНГОВОЙ ХРОНОЛОГИИ (period_chron_key из BillingPeriod.name).
    #
    #    Раньше сортировали по `period_id ASC`, предполагая что period_id
    #    монотонен по биллинговому месяцу. Это допущение СЛОМАЛОСЬ когда
    #    админ задним числом создал период «Февраль 2026» с id=90 уже после
    #    «Май 2026» (id=88). По period_id ASC получалось Апрель(id=2) →
    #    Май(id=88) → Февраль(id=90) — Февраль ставился ПОСЛЕ Мая.
    #    Размазывание дельты в шаге 5 ставило бо́льшее показание Февралю,
    #    чем Апрелю → счётчик «упал» (-4.33 у Калачёва, инцидент 28.05.2026).
    #
    #    Биллинговая хронология парсится из имени периода через
    #    period_chron_key (period_helpers.py). «Начальный период» → (0,0),
    #    обычные «Май 2026» → (2026, 5). После сортировки итерируемся в
    #    правильном хронологическом порядке независимо от порядка создания.
    rows = (await db.execute(
        select(MeterReading, BillingPeriod)
        .join(BillingPeriod, MeterReading.period_id == BillingPeriod.id)
        .where(
            MeterReading.user_id == user.id,
            MeterReading.room_id == room.id,
            MeterReading.is_approved.is_(True),
            MeterReading.id != current_reading.id,
        )
    )).all()
    history = [r for r, _p in sorted(
        rows, key=lambda row: period_chron_key(row[1].name)
    )]

    if not history:
        return None  # некомпенсировать нечего

    # 2. Идём с конца истории НАЗАД, собираем подряд AUTO-readings.
    #    Останавливаемся на первом manual — это last_manual.
    auto_chain: list[MeterReading] = []
    last_manual: Optional[MeterReading] = None
    for r in reversed(history):
        if _is_manual(r):
            last_manual = r
            break
        if _is_auto(r):
            auto_chain.append(r)
        else:
            # BASELINE / непонятный флаг → не сматчили manual, выходим
            break

    if not auto_chain or not last_manual:
        # Между last_manual и current нет AUTO-readings → коррекция не нужна.
        return None

    # Внутри auto_chain порядок от свежих к старым (мы шли reversed).
    # Возвращаем как «от старого к новому» — так удобнее накапливать дельту.
    auto_chain.reverse()

    # 3. Считаем реальную дельту от last_manual до current.
    real_dh = D(current_reading.hot_water) - D(last_manual.hot_water)
    real_dc = D(current_reading.cold_water) - D(last_manual.cold_water)
    real_de = D(current_reading.electricity or 0) - D(last_manual.electricity or 0)

    # Если cur < last_manual — это уже не «AUTO переоценил», а реальное падение.
    # Тут мы не корректируем — пусть caller обработает meter_decreased на
    # уровне gsheets/admin приёмки. На уровне skip_recalc — выходим.
    if real_dh < 0 or real_dc < 0 or real_de < 0:
        logger.warning(
            "[SKIP-RECALC] user=%s room=%s real_delta негативный — пропускаем "
            "(cur=%s/%s/%s vs last_manual=%s/%s/%s)",
            user.id, room.id,
            current_reading.hot_water, current_reading.cold_water, current_reading.electricity,
            last_manual.hot_water, last_manual.cold_water, last_manual.electricity,
        )
        return None

    # 4. NORM-only сторно-логика (28.05.2026 рефактор, Коммит 3).
    #
    #    Раньше: размазывали реальную дельту равномерно по N+1 периодам
    #    (auto_chain + current). Жилец видел «лесенку» в квитанциях —
    #    помесячно одинаковый объём, что не соответствует факту (он не
    #    пользовался в месяцы молчания, а потом разово потратил весь
    #    объём в текущем).
    #
    #    Сейчас: ПП №354 — приоритет счётчика. На auto-цепочке сторнируем
    #    ТОЛЬКО переменные cost (вода/электричество/сточные) — там жилец
    #    реально ничего не тратил по своим словам (счётчик не двигался,
    #    показания возвращаем к last_manual). Area-based (содержание,
    #    отопление, наём, ТКО) — НЕ трогаем: это ежемесячные постоянные,
    #    жилец должен их платить независимо от подачи показаний.
    #
    #    Текущий reading получает ПОЛНУЮ реальную дельту от last_manual.
    #    Жилец видит большой счёт в текущем периоде (потому что реально
    #    потратил воду за все месяцы молчания), и нулевой volume-cost
    #    на прошлых auto-месяцах. Долг/переплата в балансе считается
    #    автоматически: virtual_accrued − real_accrued ушёл в сторно.

    from app.modules.utility.services.calculations import paying_residents
    residents = D(paying_residents(user, room))
    total_room = D(room.total_room_residents or 1)
    if total_room <= 0:
        total_room = D(1)

    today = _date.today().isoformat()
    voided_volume_total = ZERO  # для аудита — сколько virtual cost снято

    # 5. Сторнируем virtual volume-cost на каждом auto-reading.
    recalced_count = 0
    for auto in auto_chain:
        # Сумма virtual volume-cost до сторно — для audit-логирования.
        voided_volume_total += (
            D(auto.cost_hot_water or 0)
            + D(auto.cost_cold_water or 0)
            + D(auto.cost_sewage or 0)
            + D(auto.cost_electricity or 0)
        )

        # Volume-cost обнуляем (жилец не пользовался в этот месяц).
        auto.cost_hot_water = ZERO
        auto.cost_cold_water = ZERO
        auto.cost_sewage = ZERO
        auto.cost_electricity = ZERO

        # Area-based (cost_maintenance / cost_fixed_part / cost_social_rent /
        # cost_waste) — НЕ ТРОГАЕМ. Они начисляются ежемесячно по площади
        # и числу жильцов, независимо от volume.

        # Показания счётчика возвращаем к last_manual: счётчик «не двигался»
        # эти месяцы. Это упрощает meter_decreased-валидацию на будущих
        # подачах: следующий реальный показатель всегда > last_manual.
        auto.hot_water = D(last_manual.hot_water)
        auto.cold_water = D(last_manual.cold_water)
        auto.electricity = D(last_manual.electricity or 0)

        # Пересчёт totals: только area-based (без volume-cost).
        auto.total_205 = D(auto.cost_social_rent or 0)
        auto.total_209 = (
            D(auto.cost_maintenance or 0)
            + D(auto.cost_fixed_part or 0)
            + D(auto.cost_waste or 0)
        )
        # Триггер integrity_002 сам синхронизирует total_cost, но выставим
        # явно для надёжности (и для in-memory consistency до flush).
        auto.total_cost = auto.total_209 + auto.total_205

        # Помечаем сторно-маркером. Сохраняем оригинальный AUTO_* флаг
        # для аудита (видно что было до сторно).
        orig_flag = (auto.anomaly_flags or "AUTO").upper()
        marker = f"VOID_VOL_{today}"
        if marker not in orig_flag:
            auto.anomaly_flags = f"{orig_flag}|{marker}"[:200]

        db.add(auto)
        recalced_count += 1

    # 6. Текущий reading: cost_* пересчитываем на ПОЛНУЮ реальную дельту
    #    от last_manual. Это даёт правильный «доплатный» счёт за весь
    #    период молчания + текущий месяц.
    elect_share = (residents / total_room) * real_de
    try:
        costs_cur = calculate_utilities(
            user=user, room=room, tariff=tariff,
            volume_hot=real_dh,
            volume_cold=real_dc,
            volume_sewage=real_dh + real_dc,
            volume_electricity_share=elect_share,
            heating_season_active=heating_season_active,
            hot_water_heating_active=hot_water_heating_active,
        )
        cost_205 = costs_cur["cost_social_rent"]
        cost_209 = costs_cur["total_cost"] - cost_205
        for k, v in costs_for_model_fields(costs_cur).items():
            setattr(current_reading, k, v)
        current_reading.total_209 = cost_209
        current_reading.total_205 = cost_205
        current_reading.total_cost = costs_cur["total_cost"]
        # Помечаем что текущий reading прошёл сторно-коррекцию.
        cur_flag = (current_reading.anomaly_flags or "").upper()
        marker = "POST_SKIP_RECALC"
        if marker not in cur_flag:
            current_reading.anomaly_flags = (
                (cur_flag + "|" + marker) if cur_flag else marker
            )[:200]
        db.add(current_reading)
    except Exception as e:
        logger.exception(
            "[SKIP-RECALC] не удалось пересчитать current reading id=%s: %s",
            current_reading.id, e,
        )

    await db.flush()

    logger.info(
        "[SKIP-RECALC] user=%s room=%s last_manual=%s voided=%d auto_chain, "
        "voided_virtual_volume_cost=%s, real_delta hot/cold/elect=%s/%s/%s",
        user.id, room.id, last_manual.id, recalced_count,
        voided_volume_total,
        real_dh, real_dc, real_de,
    )

    return {
        "auto_readings_recalced": recalced_count,
        "last_manual_period_id": last_manual.period_id,
        "real_delta_hot": real_dh,
        "real_delta_cold": real_dc,
        "real_delta_elect": real_de,
        "voided_virtual_volume_cost": voided_volume_total,
        "applied": True,
    }


__all__ = ["recalc_skip_chain"]
