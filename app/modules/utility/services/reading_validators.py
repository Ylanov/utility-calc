"""Sanity-валидация показаний счётчиков перед расчётом и сохранением.

Единый источник правды для всех 4 точек входа MeterReading:
  - mobile/web подача жильцом              (client_readings.py)
  - gsheets-import / promote                (gsheets_sync.py)
  - admin manual entry                      (admin_readings_manual.py)
  - admin approve существующего черновика   (admin_readings_approve.py)

История появления:
  В апреле 2026 на проде накопилось 50+ readings с total_cost от 100k
  до 145M ₽. Корень — жильцы записывали показание счётчика «01427.957»
  без десятичной точки → парсер получал 1 427 957 м³ → calculate_utilities
  честно умножал на тариф → счёт в миллионы. KPI «Начислено» на дашборде
  показал 1.48 млрд ₽ за один месяц (для 319 жильцов общежития).

  До этого инцидента валидации не было: парсер строки в Decimal — и
  сразу в `calculate_utilities`, без какой-либо «sanity»-проверки.

Пороги намеренно ГРУБЫЕ — отсекаем только заведомо невозможные значения:
  - вода: накопленное показание ≤ 10 000 м³ (за всю жизнь счётчика)
  - электр.: накопленное показание ≤ 50 000 кВт·ч
  - дельта за месяц: 200 м³ воды, 5 000 кВт·ч электричества
  - монотонность: новое значение ≥ предыдущего (счётчик не уменьшается)
  - неотрицательность.

Что выше порога — почти всегда ошибка ввода или тест-данные. Что ниже —
может быть валидным даже для нагретых счётчиков с долгой историей.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

# =====================================================================
# Пороги. Должны быть синхронизированы с cleanup_anomaly_readings.py.
# =====================================================================

# Абсолютное накопленное показание счётчика. Реальный жилой счётчик за
# всю жизнь набирает порядка 1 000-5 000 м³ воды. 10 000 — это «жирный»
# запас, всё что выше — гарантированно баг.
MAX_WATER_METER_VALUE = Decimal("10000")

# Электросчётчик аналогично — 50 000 кВт·ч жилое потребление за всю
# жизнь счётчика — это уже очень много.
MAX_ELECTRICITY_METER_VALUE = Decimal("50000")

# Разумный месячный расход на жилую квартиру.
# Семья 4 человека потребляет 10-20 м³ воды и 200-400 кВт·ч в месяц.
# Пороги ставим в 10× от реалистичного — отсекаем явные аномалии,
# не блокируя переезды/замены счётчика и долгие отъезды.
MAX_WATER_DELTA_PER_MONTH = Decimal("200")
MAX_ELECTRICITY_DELTA_PER_MONTH = Decimal("5000")

# Финальная sanity на total_cost. Месячный счёт за квартиру в общежитии
# при текущих тарифах: 3 000-8 000 ₽ типично, 15 000 ₽ — потолок для
# больших семей. 100 000 ₽ за месяц — это гарантированно баг.
# Используется как защита на выходе расчёта (см. calculations.py).
MAX_TOTAL_COST_PER_READING = Decimal("100000")


# =====================================================================
# Результат валидации
# =====================================================================

@dataclass
class ValidationResult:
    """Итог валидации показаний.

    `errors`   — блокирующие проблемы. Caller ДОЛЖЕН отказать в сохранении.
    `warnings` — подозрительные значения, но сохранить можно.
                 Caller помечает результат для ручной проверки админом.
    """
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok


# =====================================================================
# Главный валидатор
# =====================================================================

def validate_meter_reading(
    *,
    hot: Optional[Decimal],
    cold: Optional[Decimal],
    elect: Optional[Decimal],
    prev_hot: Optional[Decimal] = None,
    prev_cold: Optional[Decimal] = None,
    prev_elect: Optional[Decimal] = None,
    is_baseline: bool = False,
) -> ValidationResult:
    """Проверяет ОДНУ подачу показаний счётчика.

    Параметры:
      hot/cold/elect       — новые значения (из подачи). None допустимо
                             только для elect (gsheets его не передаёт).
      prev_hot/cold/elect  — предыдущие утверждённые значения для дельты.
                             None означает «нет истории» — значит, дельту
                             не проверяем (см. is_baseline).
      is_baseline          — первая подача счётчика. Дельта от 0 не
                             проверяется (там может быть «накрученное»
                             значение от прежних жильцов).

    Возвращает ValidationResult. Не raise'ит — caller сам решает что
    делать с errors (HTTPException, mark conflict, skip, ...).
    """
    result = ValidationResult()

    # 1. Не-null для воды (gsheets иногда не передаёт electricity — ОК).
    if hot is None:
        result.errors.append("hot_water не задан")
    if cold is None:
        result.errors.append("cold_water не задан")

    # 2. Неотрицательность.
    for name, value in [("hot_water", hot), ("cold_water", cold), ("electricity", elect)]:
        if value is not None and value < 0:
            result.errors.append(f"{name} не может быть отрицательным: {value}")

    # 3. Абсолютный потолок (защита от пропущенной десятичной точки).
    for name, value, ceiling in [
        ("hot_water", hot, MAX_WATER_METER_VALUE),
        ("cold_water", cold, MAX_WATER_METER_VALUE),
        ("electricity", elect, MAX_ELECTRICITY_METER_VALUE),
    ]:
        if value is not None and value > ceiling:
            result.errors.append(
                f"{name}={value} превышает максимум {ceiling}. "
                f"Возможно, в показании пропущена десятичная точка "
                f"(например, '01427.957' введено как '01427957')."
            )

    if not result.ok:
        return result  # дальше проверять смысла нет — данные уже бракованы

    # 4. Монотонность: счётчик не может уменьшаться.
    # Обходим, если is_baseline — там сравнивать не с чем (предыдущие
    # значения могут быть «грязные» от прошлых жильцов).
    if not is_baseline:
        for name, value, prev in [
            ("hot_water", hot, prev_hot),
            ("cold_water", cold, prev_cold),
            ("electricity", elect, prev_elect),
        ]:
            if value is not None and prev is not None and value < prev:
                result.errors.append(
                    f"{name}={value} меньше предыдущего значения {prev}. "
                    f"Счётчик не может уменьшаться. Проверьте номер счётчика "
                    f"(возможно, его меняли — нужна процедура «Замена счётчика»)."
                )

    # 5. Дельта за месяц. Только если есть prev и не baseline.
    if not is_baseline:
        for name, value, prev, max_delta in [
            ("hot_water", hot, prev_hot, MAX_WATER_DELTA_PER_MONTH),
            ("cold_water", cold, prev_cold, MAX_WATER_DELTA_PER_MONTH),
            ("electricity", elect, prev_elect, MAX_ELECTRICITY_DELTA_PER_MONTH),
        ]:
            if value is None or prev is None:
                continue
            delta = value - prev
            if delta > max_delta:
                # Это warning, не error — теоретически возможны редкие
                # кейсы (долгое отсутствие, потом массовая подача за 6 мес).
                # Caller (например, mobile UI) может попросить подтверждение.
                result.warnings.append(
                    f"{name}: расход {delta} за период превышает обычный "
                    f"месячный максимум {max_delta}. Проверьте показание."
                )

    return result


def validate_total_cost(total: Optional[Decimal]) -> ValidationResult:
    """Sanity на ИТОГ расчёта. Используется на выходе calculate_utilities,
    чтобы не сохранять в БД заведомо аномальный результат даже если
    входные значения как-то прошли валидацию.

    100k ₽/месяц для общежития — это гарантированно баг (типичный счёт
    3-8k ₽). Реальные тарифы × реальные показания этого не дают.
    """
    result = ValidationResult()
    if total is None:
        return result
    if total > MAX_TOTAL_COST_PER_READING:
        result.errors.append(
            f"total_cost={total} превышает санитарный потолок "
            f"{MAX_TOTAL_COST_PER_READING} ₽/период. "
            f"Скорее всего, проблема в показаниях или в тарифе."
        )
    return result
