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
    "ONE_TIME_CHARGE",
    "ONE_TIME_CHARGE_BASELINE",
    "PENDING",
})


def real_flags(flags_csv: str | None) -> list[str]:
    """Возвращает только настоящие флаги аномалий из CSV-строки.

    Отбрасывает source-маркеры и пустые токены. Пример:

        real_flags("AUTO_GENERATED,SPIKE_HOT,PENDING") -> ["SPIKE_HOT"]
        real_flags("AUTO_GENERATED")                   -> []
        real_flags(None)                               -> []
    """
    if not flags_csv:
        return []
    return [
        token.strip()
        for token in flags_csv.split(",")
        if token.strip() and token.strip() not in SOURCE_MARKERS
    ]


def has_real_anomaly(flags_csv: str | None) -> bool:
    """True если в строке есть хотя бы один настоящий флаг (не source-маркер)."""
    return bool(real_flags(flags_csv))
