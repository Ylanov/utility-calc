"""anomaly_flags.py — общие константы и утилиты для работы с
MeterReading.anomaly_flags (CSV-строка типа 'SPIKE_HOT,FLAT_COLD').

Раньше каждый модуль (admin_analyzer.py, admin_reports.py, flag-heatmap)
определял свой локальный список «служебных» маркеров — это приводило к
расхождениям. Например, KPI «Аномалий найдено: 583» брал данные из
analyzer_dashboard, который считал AUTO_GENERATED как аномалию, а Inbox
её правильно фильтровал — KPI показывал 583, Inbox 0.

Единое место правды.
"""
from __future__ import annotations


# Маркеры источника записи, НЕ аномалии. Если у MeterReading.anomaly_flags
# только эти токены — запись чистая, никаких проблем для админа.
#
# Откуда что приходит:
#   GSHEETS_AUTO           — auto-approve gsheets-импорта (high match score)
#   GSHEETS_AUTO_BASELINE  — baseline-reading из gsheets для жильца который
#                            подал первый раз
#   GSHEETS_IMPORT         — обычный pending-импорт из gsheets
#   BASELINE               — начальное показание счётчика
#   AUTO_GENERATED         — сгенерировано без подачи (initial setup / fill)
#   INITIAL_SETUP          — initial-readings admin endpoint
#   DATA_OVERFLOW_RESET    — обнулено cleanup_anomaly_readings.py (score=100)
#   ONE_TIME_CHARGE        — разовое начисление (admin_adjustments)
#   ONE_TIME_CHARGE_BASELINE — baseline для разового начисления
#   PENDING                — placeholder во время обработки
SOURCE_MARKERS: frozenset[str] = frozenset({
    "GSHEETS_AUTO",
    "GSHEETS_AUTO_BASELINE",
    "GSHEETS_IMPORT",
    "BASELINE",
    "AUTO_GENERATED",
    "INITIAL_SETUP",
    "DATA_OVERFLOW_RESET",
    "ADMIN_APPROVED_OVERFLOW",  # админ утвердил ранее заблокированный
    "ONE_TIME_CHARGE",
    "ONE_TIME_CHARGE_BASELINE",
    "PENDING",
    # Маркеры авто-начисления невозвратчикам (см. billing.close_current_period).
    # Не аномалии — это нормальное поведение системы при пропуске жильцом подачи.
    "AUTO_AVG",
    "AUTO_AVG_FALLBACK",
    "AUTO_NORM_SANCTION",
    "AUTO_NO_HISTORY",
    "MANUAL_RECEIPT",     # admin создал квитанцию вручную (без подачи показаний)
    "POST_SKIP_RECALC",   # маркер reading'а прошедшего retroactive recalc
    "STATIC_RENT",        # статичное начисление наёма дому (place_type=house), без счётчиков
})

# Маркеры-префиксы (data-patches помеченные датой). Используется
# is_source_marker() для prefix-проверки, потому что SOURCE_MARKERS — frozenset
# и не поддерживает шаблоны. Эти маркеры — следы ручной коррекции данных
# админом, не аномалии-инциденты.
_SOURCE_PREFIXES: tuple[str, ...] = (
    "BASELINE_LEGACY",   # BASELINE_LEGACY_FALSE_POSITIVE_PATCHED_2026_05_20,
                         # BASELINE_LEGACY_APR_2026_MASS_PATCH и т.д.
    "RECALCED_",         # данные пересчитаны (через retroactive recalc)
)


def is_source_marker(token: str) -> bool:
    """True если токен — служебный source-маркер (включая prefix-патчи)."""
    if not token:
        return False
    t = token.strip()
    if t in SOURCE_MARKERS:
        return True
    return any(t.startswith(p) for p in _SOURCE_PREFIXES)


def real_flags(flags_csv: str | None) -> list[str]:
    """Возвращает только настоящие флаги аномалий из CSV-строки.

    Отбрасывает source-маркеры и пустые токены. Пример:

        real_flags("AUTO_GENERATED,SPIKE_HOT,PENDING") -> ["SPIKE_HOT"]
        real_flags("AUTO_GENERATED")                   -> []
        real_flags("MANUAL_RECEIPT")                   -> []
        real_flags("BASELINE_LEGACY_APR_2026")         -> []
        real_flags("SPIKE_HOT|RECALCED_2026-05-20")    -> ["SPIKE_HOT"]
        real_flags(None)                               -> []
    """
    if not flags_csv:
        return []
    # CSV-формат: разделитель ','. Также поддерживаем '|' (legacy формат
    # из skip_recalc, где "AUTO_AVG|RECALCED_2026-05-20").
    raw = flags_csv.replace("|", ",")
    return [
        token.strip()
        for token in raw.split(",")
        if token.strip() and not is_source_marker(token.strip())
    ]


def has_real_anomaly(flags_csv: str | None) -> bool:
    """True если в строке есть хотя бы один настоящий флаг (не source-маркер)."""
    return bool(real_flags(flags_csv))


# Маркеры МАШИННОГО начисления (без подачи жильца): система оценила
# потребление по нормативу × проживающих (×3 после 3 пропусков) вместо
# реальных показаний счётчика. Жилец видит в приложении бейдж «Начислено
# по нормативу» vs «По вашим показаниям».
#
# ВНИМАНИЕ: это ОТДЕЛЬНЫЙ концепт от _is_auto/AUTO_FLAGS в billing.py
# (подсчёт miss_count) и от PREV_SKIP_FLAGS в reading_calculator.py
# (исключение синтетики из prev). Здесь — только «начислено ли это
# машиной по нормативу/среднему» для разграничения в UI. AUTO_GENERATED
# (нулевой baseline, начисления нет) сюда НЕ входит.
ESTIMATED_CHARGE_FLAGS: frozenset[str] = frozenset({
    "AUTO_NORM",
    "AUTO_NORM_SANCTION",
    "AUTO_AVG",            # legacy (стратегия удалена 28.05.2026, но в истории есть)
    "AUTO_AVG_FALLBACK",   # legacy
    "AUTO_NO_HISTORY",
})


def is_estimated_charge(flags_csv: str | None) -> bool:
    """True если начисление сделано МАШИНОЙ по нормативу/среднему (жилец не
    подавал показания). Substring-проверка по ESTIMATED_CHARGE_FLAGS: флаги
    в anomaly_flags идут CSV-строкой с другими токенами и в любом регистре.

        is_estimated_charge("AUTO_NORM")            -> True
        is_estimated_charge("AUTO_NORM_SANCTION")   -> True
        is_estimated_charge("PENDING,SPIKE_HOT")    -> False  (реальная подача)
        is_estimated_charge("BASELINE")             -> False
        is_estimated_charge(None)                   -> False
    """
    if not flags_csv:
        return False
    up = flags_csv.upper()
    return any(f in up for f in ESTIMATED_CHARGE_FLAGS)
