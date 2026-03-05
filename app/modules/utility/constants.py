# app/modules/utility/constants.py
from typing import Dict

# Карта для детализации аномалий
ANOMALY_MAP: Dict[str, Dict[str, str]] = {
    "NEGATIVE_HOT": {"message": "Ошибка: Текущие показания ГВС меньше предыдущих!", "severity": "high"},
    "NEGATIVE_COLD": {"message": "Ошибка: Текущие показания ХВС меньше предыдущих!", "severity": "high"},
    "NEGATIVE_ELECT": {"message": "Ошибка: Текущие показания электричества меньше предыдущих!", "severity": "high"},
    "HIGH_HOT": {"message": "Очень высокий расход горячей воды по сравнению с историей.", "severity": "medium"},
    "HIGH_COLD": {"message": "Очень высокий расход холодной воды по сравнению с историей.", "severity": "medium"},
    "HIGH_ELECT": {"message": "Очень высокий расход электричества по сравнению с историей.", "severity": "medium"},
    "HIGH_VS_PEERS_HOT": {"message": "Расход ГВС значительно выше среднего по общежитию.", "severity": "medium"},
    "HIGH_VS_PEERS_COLD": {"message": "Расход ХВС значительно выше среднего по общежитию.", "severity": "medium"},
    "HIGH_VS_PEERS_ELECT": {"message": "Расход электричества значительно выше среднего по общежитию.", "severity": "medium"},
    "ZERO_HOT": {"message": "Нулевой расход горячей воды (возможно, комната пустует).", "severity": "low"},
    "ZERO_COLD": {"message": "Нулевой расход холодной воды (возможно, комната пустует).", "severity": "low"},
    "ZERO_ELECT": {"message": "Нулевой расход электричества (возможно, ком-та пустует).", "severity": "low"},
    "FROZEN_HOT": {"message": "Показания счетчика ГВС не менялись 3+ месяца.", "severity": "low"},
    "FROZEN_COLD": {"message": "Показания счетчика ХВС не менялись 3+ месяца.", "severity": "low"},
    "FROZEN_ELECT": {"message": "Показания счетчика света не менялись 3+ месяца.", "severity": "low"},
    "UNKNOWN": {"message": "Обнаружена неопознанная аномалия.", "severity": "low"}
}