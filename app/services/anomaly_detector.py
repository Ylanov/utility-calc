from typing import List, Optional, Dict
from statistics import mean, stdev
from decimal import Decimal
from app.models import MeterReading


# Вспомогательная функция для гарантии Decimal
def D(val) -> Decimal:
    if val is None:
        return Decimal("0.000")
    if isinstance(val, float):
        return Decimal(str(val))
    return Decimal(val)


def check_reading_for_anomalies(
        current_reading: MeterReading,
        history: List[MeterReading],
        avg_peer_consumption: Optional[Dict[str, Decimal]] = None
) -> Optional[str]:
    """
    Анализирует показание на основе истории и возвращает строку с флагами аномалий.
    Использует Decimal для точных сравнений.
    """
    if not history or len(history) < 2:
        return None  # Недостаточно данных для анализа

    flags = []

    # Константы для порогов в Decimal
    ZERO = Decimal("0.000")
    THRESHOLD_SMALL = Decimal("0.1")
    THRESHOLD_ELECT = Decimal("1.0")
    THRESHOLD_PEER_ELECT = Decimal("10.0")
    FACTOR_STD_DEV = Decimal("2.5")
    FACTOR_PEER = Decimal("3")

    # --- Считаем исторические дельты (расход по месяцам) ---
    deltas = {
        'hot': [],
        'cold': [],
        'elect': []
    }
    for i in range(len(history) - 1):
        # history отсортирован от нового к старому
        curr = history[i]
        prev = history[i + 1]

        # Вычисляем разницу, используя D() для безопасности
        d_hot = max(ZERO, D(curr.hot_water) - D(prev.hot_water))
        d_cold = max(ZERO, D(curr.cold_water) - D(prev.cold_water))
        d_elect = max(ZERO, D(curr.electricity) - D(prev.electricity))

        deltas['hot'].append(d_hot)
        deltas['cold'].append(d_cold)
        deltas['elect'].append(d_elect)

    # --- Текущий расход ---
    last_approved = history[0]

    current_delta_hot = D(current_reading.hot_water) - D(last_approved.hot_water)
    current_delta_cold = D(current_reading.cold_water) - D(last_approved.cold_water)
    current_delta_elect = D(current_reading.electricity) - D(last_approved.electricity)

    # --- НОВОЕ ПРАВИЛО 0: ОТРИЦАТЕЛЬНЫЙ РАСХОД (ВЫСШИЙ ПРИОРИТЕТ) ---
    if current_delta_hot < 0: flags.append("NEGATIVE_HOT")
    if current_delta_cold < 0: flags.append("NEGATIVE_COLD")
    if current_delta_elect < 0: flags.append("NEGATIVE_ELECT")

    # --- ПРАВИЛО 1: Нулевое потребление ---
    # mean() корректно работает со списком Decimal
    if current_delta_hot == 0 and mean(deltas['hot']) > THRESHOLD_SMALL:
        flags.append("ZERO_HOT")

    if current_delta_cold == 0 and mean(deltas['cold']) > THRESHOLD_SMALL:
        flags.append("ZERO_COLD")

    if current_delta_elect == 0 and mean(deltas['elect']) > THRESHOLD_ELECT:
        flags.append("ZERO_ELECT")

    # --- ПРАВИЛО 2: Статистический выброс (более 2.5 стандартных отклонений) ---
    if len(history) >= 3:  # Нужно хотя бы 2 дельты для stdev
        for res_type in ['hot', 'cold', 'elect']:
            hist_deltas = deltas[res_type]
            if not hist_deltas: continue

            # Если все значения одинаковы, stdev может упасть или быть 0
            if len(set(hist_deltas)) == 1:
                continue

            avg = mean(hist_deltas)
            try:
                std_dev = stdev(hist_deltas)
            except Exception:
                std_dev = avg  # Fallback

            # Устанавливаем порог
            threshold = avg + (FACTOR_STD_DEV * std_dev) + THRESHOLD_SMALL

            # Получаем текущую дельту динамически
            if res_type == 'hot':
                current_delta = current_delta_hot
            elif res_type == 'cold':
                current_delta = current_delta_cold
            else:
                current_delta = current_delta_elect

            if current_delta > threshold and current_delta > THRESHOLD_ELECT:
                flags.append(f"HIGH_{res_type.upper()}")

    # --- ПРАВИЛО 3: "Замерзший" счетчик (3 последних показания одинаковы) ---
    if len(history) >= 2:
        # Прямое сравнение Decimal безопасно
        if D(history[0].hot_water) == D(history[1].hot_water) == D(current_reading.hot_water):
            flags.append("FROZEN_HOT")
        if D(history[0].cold_water) == D(history[1].cold_water) == D(current_reading.cold_water):
            flags.append("FROZEN_COLD")
        if D(history[0].electricity) == D(history[1].electricity) == D(current_reading.electricity):
            flags.append("FROZEN_ELECT")

    if not flags:
        return None

    # --- ПРАВИЛО 4: Сравнение со средним по группе (общежитию) ---
    if avg_peer_consumption:
        # Безопасно получаем значения из словаря, приводя к Decimal
        avg_hot = D(avg_peer_consumption.get('avg_hot', 0))
        avg_cold = D(avg_peer_consumption.get('avg_cold', 0))
        avg_elect = D(avg_peer_consumption.get('avg_elect', 0))

        if avg_hot > THRESHOLD_ELECT and current_delta_hot > avg_hot * FACTOR_PEER:
            flags.append("HIGH_VS_PEERS_HOT")

        if avg_cold > THRESHOLD_ELECT and current_delta_cold > avg_cold * FACTOR_PEER:
            flags.append("HIGH_VS_PEERS_COLD")

        if avg_elect > THRESHOLD_PEER_ELECT and current_delta_elect > avg_elect * FACTOR_PEER:
            flags.append("HIGH_VS_PEERS_ELECT")

    if not flags:
        return None

    return ",".join(sorted(list(set(flags))))