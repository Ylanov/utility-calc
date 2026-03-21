# app/modules/utility/constants.py
from typing import Dict

# Карта для детализации аномалий (Risk Engine)
ANOMALY_MAP: Dict[str, Dict[str, str]] = {
    # КРИТИЧЕСКИЕ (Risk +50..100)
    "NEGATIVE": {"message": "Счетчик скручен (отрицательный расход)!", "severity": "critical", "color": "#dc2626"},

    # ПОВЕДЕНЧЕСКИЕ / ФРОД (Risk +30..40)
    "DROP_AFTER_SPIKE": {"message": "Резкое падение после выброса (попытка скрыть расход)", "severity": "high",
                         "color": "#b91c1c"},
    "FLAT": {"message": "Подозрительно одинаковый расход (рисуют цифры)", "severity": "high", "color": "#ea580c"},
    "FROZEN": {"message": "Показания счетчика не меняются (замерз/сломан)", "severity": "medium", "color": "#0284c7"},
    "TREND_UP": {"message": "Постоянный рост расхода 4 месяца подряд (возможна скрытая утечка)", "severity": "high",
                 "color": "#c2410c"},

    # СТАТИСТИЧЕСКИЕ (Risk +20..40)
    "SPIKE": {"message": "Аномальный скачок расхода (выброс)", "severity": "high", "color": "#e11d48"},
    "HIGH": {"message": "Расход выше нормы для этого жильца", "severity": "medium", "color": "#f59e0b"},
    "ZERO": {"message": "Нулевой расход (хотя раньше потреблял)", "severity": "low", "color": "#64748b"},

    # КОНТЕКСТНЫЕ (Risk +20)
    "HIGH_PER_PERSON": {"message": "Критический перерасход на 1 прописанного человека", "severity": "high",
                        "color": "#9333ea"},
    "HIGH_VS_PEERS": {"message": "Расход значительно выше среднего по общежитию", "severity": "medium",
                      "color": "#7c3aed"},

    # СИСТЕМНЫЕ
    "AUTO_GENERATED": {"message": "Начислено по среднему (системой)", "severity": "info", "color": "#0ea5e9"},
    "ONE_TIME_CHARGE": {"message": "Разовое начисление (выселение)", "severity": "info", "color": "#8b5cf6"},
    "IMPORTED_DRAFT": {"message": "Загружено из Excel", "severity": "info", "color": "#10b981"},
    "UNKNOWN": {"message": "Неизвестная аномалия", "severity": "low", "color": "#9ca3af"}
}