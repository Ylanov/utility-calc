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

    # 4. Делим равномерно. N = len(auto_chain) + 1 (включая current).
    N = D(len(auto_chain) + 1)
    avg_dh = real_dh / N
    avg_dc = real_dc / N
    avg_de = real_de / N

    # 5. Перезаписываем каждый AUTO-reading. Накапливаем от last_manual.
    running_hot = D(last_manual.hot_water)
    running_cold = D(last_manual.cold_water)
    running_elect = D(last_manual.electricity or 0)

    residents = D(user.residents_count or 1)
    total_room = D(room.total_room_residents or 1)
    if total_room <= 0:
        total_room = D(1)

    recalced_count = 0
    for i, auto in enumerate(auto_chain, start=1):
        # На i-м auto-month прибавляем одну долю.
        new_hot = running_hot + avg_dh
        new_cold = running_cold + avg_dc
        new_elect = running_elect + avg_de

        # Считаем cost этого периода с актуальным тарифом и avg-дельтой.
        elect_share = (residents / total_room) * avg_de
        try:
            costs = calculate_utilities(
                user=user, room=room, tariff=tariff,
                volume_hot=avg_dh, volume_cold=avg_dc,
                volume_sewage=avg_dh + avg_dc,
                volume_electricity_share=elect_share,
                heating_season_active=heating_season_active,
                hot_water_heating_active=hot_water_heating_active,
            )
        except Exception as e:
            logger.exception(
                "[SKIP-RECALC] не удалось пересчитать reading id=%s: %s",
                auto.id, e,
            )
            continue

        cost_205 = costs["cost_social_rent"]
        cost_209 = costs["total_cost"] - cost_205

        # Накопленные показания счётчика обновляем.
        auto.hot_water = new_hot
        auto.cold_water = new_cold
        auto.electricity = new_elect
        # Cost-компоненты обновляются полностью.
        for k, v in costs_for_model_fields(costs).items():
            setattr(auto, k, v)
        auto.total_209 = cost_209
        auto.total_205 = cost_205
        auto.total_cost = costs["total_cost"]

        # Помечаем в flag, что reading прошёл retroactive recalc.
        # Сохраняем «генезис» (AUTO_AVG или AUTO_NORM_SANCTION) для аудита.
        orig_flag = (auto.anomaly_flags or "AUTO").upper()
        today = _date.today().isoformat()
        auto.anomaly_flags = f"{orig_flag}|RECALCED_{today}"[:200]  # лимит для column

        db.add(auto)
        recalced_count += 1
        running_hot = new_hot
        running_cold = new_cold
        running_elect = new_elect

    # 6. Текущий reading тоже обновляем — он получает одну avg-долю
    #    (а не «всё что было пропущено»). Без этого жилец увидел бы
    #    весь долг пропусков в текущей квитанции.
    elect_share = (residents / total_room) * avg_de
    try:
        costs_cur = calculate_utilities(
            user=user, room=room, tariff=tariff,
            volume_hot=avg_dh, volume_cold=avg_dc,
            volume_sewage=avg_dh + avg_dc,
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
        # total_cost у current может включать долги/корректировки —
        # caller сам решит. Тут только base.
        current_reading.total_cost = costs_cur["total_cost"]
        # Помечаем что текущий reading прошёл коррекцию.
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
        "[SKIP-RECALC] user=%s room=%s last_manual=%s recalced=%d auto_chain, "
        "real_dh=%s real_dc=%s real_de=%s avg per month: %s/%s/%s",
        user.id, room.id, last_manual.id, recalced_count,
        real_dh, real_dc, real_de, avg_dh, avg_dc, avg_de,
    )

    return {
        "auto_readings_recalced": recalced_count,
        "last_manual_period_id": last_manual.period_id,
        "real_delta_hot": real_dh,
        "real_delta_cold": real_dc,
        "real_delta_elect": real_de,
        "avg_delta_hot": avg_dh,
        "avg_delta_cold": avg_dc,
        "avg_delta_elect": avg_de,
        "applied": True,
    }


__all__ = ["recalc_skip_chain"]
