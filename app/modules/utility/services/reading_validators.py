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

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

# =====================================================================
# Пороги (DEFAULTS). Реальные значения читаются через analyzer_config —
# админ может менять без редеплоя. Константы остаются как fallback и для
# скриптов / тестов где analyzer_settings таблица недоступна.
# =====================================================================

# Абсолютное накопленное показание счётчика — потолок ввода. Был 10000,
# но при формате 5_3_strict пользователь может ввести до 99999.999.
# Реальные счётчики в общежитиях редко доходят до 5-значных значений,
# но для электр. — обычное дело. Bug AW1: расширен до 99999.999 (вода)
# и 9_999_999 (электр — счётчик 7+ разрядов).
MAX_WATER_METER_VALUE = Decimal("99999.999")
MAX_ELECTRICITY_METER_VALUE = Decimal("9999999")

# Разумный месячный расход. 50 м³ — щедрый потолок для общежития (4 чел ×
# 4 м³/мес × 3х запас). Был 200 — это «жилая квартира с гостями круглый
# год», для нашего фонда нереалистично. Дельта > порога — ERROR, не warning:
# инцидент с Пегарьковым (май 2026) — +236 м³ ХВС/мес прошло как warning,
# счёт составил 81 485 ₽. Теперь такое блокируется на входе.
MAX_WATER_DELTA_PER_MONTH = Decimal("50")
MAX_ELECTRICITY_DELTA_PER_MONTH = Decimal("2000")

# Потолок для ПЕРВОЙ подачи (is_baseline=True). Если счётчик НОВЫЙ — у него
# должно быть ~0. Если счётчик СТАРЫЙ и в нём накручено — это нормально:
# при baseline нет «прошлого», от которого считать дельту, поэтому начисление
# = 0 (только фикс-часть). Bug AW1: расширен до 99999.999 — пользователь
# часто запускает систему когда у счётчиков уже накручено 5-значное значение,
# и блокировать это ошибочно.
MAX_FIRST_SUBMISSION_VALUE = Decimal("99999.999")

# Финальная sanity на total_cost.
MAX_TOTAL_COST_PER_READING = Decimal("100000")


def _threshold(key: str, default: Decimal) -> Decimal:
    """Читает порог из analyzer_config с fallback на default-константу.

    Делает Decimal-safe чтение: даже если в БД сохранена строка вида
    "10000" — корректно парсится. При недоступности БД (тесты, скрипты)
    возвращает default.
    """
    try:
        from app.modules.utility.services.analyzer_config import config
        v = config.get_float(key, default=float(default))
        return Decimal(str(v))
    except Exception:
        return default


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
# Строгая проверка raw-формата (для format=5_3_strict)
# =====================================================================

# Pattern: 1-5 цифр до точки + точка + ровно 3 цифры после.
# Допускаем 1-5 цифр до точки (а не строго 5) потому что HTML number-input
# отбрасывает ведущие нули при отправке; auto-format на клиенте их добавит,
# но если каким-то образом не сработает — серверный pattern всё равно
# отсеет «1.4» (2 цифры) и «1234567.890» (7 цифр).
STRICT_5_3_PATTERN = re.compile(r"^\d{1,5}\.\d{3}$")


def validate_raw_format(raw: Optional[str], fmt: str) -> Optional[str]:
    """Возвращает None если raw подходит формату, иначе строку с ошибкой.

    Используется ТОЛЬКО для format='5_3_strict' (жёсткий 5+3).
    Для 5_no_decimal / 5_with_decimal / any — формат свободный,
    эта функция возвращает None.
    """
    if fmt != "5_3_strict":
        return None
    if not raw:
        return "значение не задано"
    s = str(raw).strip().replace(",", ".")
    if not STRICT_5_3_PATTERN.match(s):
        return (
            f"значение '{raw}' не в формате 5+3 (пример: 01427.957). "
            "Введите ровно 8 цифр счётчика — 5 целых и 3 дробных через точку. "
            "Если число короткое, допишите ведущие нули и нули после точки."
        )
    return None


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
    prev_is_synth: bool = False,
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
                             значение от прежних жильцов). Но новое значение
                             ограничено MAX_FIRST_SUBMISSION_VALUE (50 м³) —
                             иначе админ должен сначала заполнить «Начальный
                             период» через ручной ввод.
      prev_is_synth        — есть предыдущий reading, но он SYNTH (AUTO_GENERATED
                             0/0/0 или DATA_OVERFLOW_RESET). В этом случае
                             prev_hot/prev_cold подаются как РЕАЛЬНЫЕ значения
                             синта (т.е. 0), но мы НЕ доверяем дельте от него
                             как «нормальной», и проверка delta включается с
                             более строгим порогом MAX_FIRST_SUBMISSION_VALUE.
                             Это покрывает случай Пегарькова — есть baseline
                             0/0/0, новое значение 161/340, дельта 161/340 >>
                             порога → ERROR с инструкцией поправить baseline.

    Возвращает ValidationResult. Не raise'ит — caller сам решает что
    делать с errors (HTTPException, mark conflict, skip, ...).
    """
    result = ValidationResult()

    # Динамические пороги из analyzer_config (с fallback на defaults).
    max_water = _threshold("validator.max_water_meter", MAX_WATER_METER_VALUE)
    max_elect = _threshold("validator.max_electricity_meter", MAX_ELECTRICITY_METER_VALUE)
    max_water_delta = _threshold("validator.max_water_delta_per_month", MAX_WATER_DELTA_PER_MONTH)
    max_elect_delta = _threshold("validator.max_electricity_delta_per_month", MAX_ELECTRICITY_DELTA_PER_MONTH)
    max_first_submission = _threshold("validator.max_first_submission_value", MAX_FIRST_SUBMISSION_VALUE)

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
        ("hot_water", hot, max_water),
        ("cold_water", cold, max_water),
        ("electricity", elect, max_elect),
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
    # Bug E2-D (28.05.2026, Вастаев): ТАКЖЕ обходим если prev_is_synth=True.
    # Synth-prev — это AUTO_AVG / AUTO_NORM_SANCTION / DATA_OVERFLOW_RESET,
    # которые насчитала сама система. Если жилец потом подаёт реальные
    # показания за БОЛЕЕ РАННИЙ месяц и они меньше synth — это значит
    # «AUTO переоценил», НЕ «счётчик упал». Caller (admin_gsheets._apply_approve
    # → recalc_skip_chain) ретроактивно пересчитает synth-цепочку.
    if not is_baseline and not prev_is_synth:
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

    # 5. Дельта за месяц. Проверяется когда:
    #    - есть реальный prev (не baseline) → обычный порог;
    #    - prev_is_synth=True → дельта проверяется СТРОЖЕ (от 0 или DATA_OVERFLOW_RESET),
    #      потому что «реальная» история отсутствует и любое большое
    #      значение тут — индикатор криво поставленного baseline.
    delta_check_active = (not is_baseline) or prev_is_synth
    if delta_check_active:
        # При synth-baseline режем по жёсткому порогу первой подачи —
        # это спасает от Пегарькова (+236 м³ ХВС с AUTO_GENERATED prev=0).
        effective_water_delta = max_first_submission if prev_is_synth else max_water_delta
        effective_elect_delta = max_elect_delta if not prev_is_synth else max_first_submission * Decimal("100")  # электричество всё-таки не вода

        for name, value, prev, max_delta in [
            ("hot_water", hot, prev_hot, effective_water_delta),
            ("cold_water", cold, prev_cold, effective_water_delta),
            ("electricity", elect, prev_elect, effective_elect_delta),
        ]:
            if value is None or prev is None:
                continue
            delta = value - prev
            if delta > max_delta:
                # ERROR (раньше warning): пропустить такую дельту = инцидент
                # с Пегарьковым / Капрановым (счёт 81k-825k ₽). Если кейс
                # реально валидный (долгое отсутствие, массовая подача за
                # 6 мес), админ внесёт показание через «Начальный период» /
                # «Замена счётчика» — там корректный путь.
                if prev_is_synth:
                    result.errors.append(
                        f"{name}: дельта {delta} от synth-baseline превышает "
                        f"порог {max_delta}. Установите корректное «Начальное "
                        f"показание» в Ручном вводе (это значение, которое "
                        f"было на счётчике в момент привязки жильца), затем "
                        f"переподайте показания."
                    )
                else:
                    result.errors.append(
                        f"{name}: расход {delta} за период превышает "
                        f"месячный максимум {max_delta}. Если это валидная "
                        f"подача за несколько месяцев — оформите через "
                        f"«Замену счётчика» или установите промежуточные "
                        f"показания."
                    )

    # 6. Защита от is_baseline-абюза: если жилец/админ подаёт «первую»
    # подачу с гигантским значением (накопленное показание счётчика без
    # установки baseline), блокируем. См. MAX_FIRST_SUBMISSION_VALUE.
    if is_baseline and not prev_is_synth:
        for name, value in [("hot_water", hot), ("cold_water", cold)]:
            if value is not None and value > max_first_submission:
                result.errors.append(
                    f"{name}={value} слишком велико для первой подачи "
                    f"(порог {max_first_submission}). Скорее всего у счётчика "
                    f"уже накручено показание из истории — установите его "
                    f"через «Начальный период» в Ручном вводе, и только "
                    f"после этого подавайте текущие значения."
                )
        if elect is not None and elect > max_elect_delta:
            result.errors.append(
                f"electricity={elect} слишком велико для первой подачи. "
                f"Установите начальное показание через «Начальный период»."
            )

    return result


def validate_total_cost(total: Optional[Decimal]) -> ValidationResult:
    """Sanity на ИТОГ расчёта. Используется на выходе calculate_utilities,
    чтобы не сохранять в БД заведомо аномальный результат даже если
    входные значения как-то прошли валидацию.

    Порог редактируется через analyzer_config — admin меняет «нормальный
    потолок счёта» без редеплоя.
    """
    result = ValidationResult()
    if total is None:
        return result
    ceiling = _threshold("validator.max_total_cost_per_reading", MAX_TOTAL_COST_PER_READING)
    if total > ceiling:
        result.errors.append(
            f"total_cost={total} превышает санитарный потолок "
            f"{ceiling} ₽/период. "
            f"Скорее всего, проблема в показаниях или в тарифе."
        )
    return result


def get_max_total_cost_per_reading() -> Decimal:
    """Public getter для других модулей (calculations.py для sanity_warning).
    Использует тот же analyzer_config-ключ что и validate_total_cost —
    единая точка истины (раньше были две константы в разных файлах).
    """
    return _threshold("validator.max_total_cost_per_reading", MAX_TOTAL_COST_PER_READING)
