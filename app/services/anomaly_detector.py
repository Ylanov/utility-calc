from typing import List, Optional, Dict
from statistics import mean, stdev
from app.models import MeterReading


def check_reading_for_anomalies(
    current_reading: MeterReading,
    history: List[MeterReading],
    avg_peer_consumption: Optional[Dict[str, float]] = None
) -> Optional[str]:
    """
    Анализирует показание на основе истории и возвращает строку с флагами аномалий.
    """
    if not history or len(history) < 2:
        return None  # Недостаточно данных для анализа

    flags = []

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
        deltas['hot'].append(max(0, curr.hot_water - prev.hot_water))
        deltas['cold'].append(max(0, curr.cold_water - prev.cold_water))
        deltas['elect'].append(max(0, curr.electricity - prev.electricity))

    # --- Текущий расход ---
    last_approved = history[0]
    current_delta_hot = current_reading.hot_water - last_approved.hot_water
    current_delta_cold = current_reading.cold_water - last_approved.cold_water
    current_delta_elect = current_reading.electricity - last_approved.electricity

    # --- НОВОЕ ПРАВИЛО 0: ОТРИЦАТЕЛЬНЫЙ РАСХОД (ВЫСШИЙ ПРИОРИТЕТ) ---
    if current_delta_hot < 0: flags.append("NEGATIVE_HOT")
    if current_delta_cold < 0: flags.append("NEGATIVE_COLD")
    if current_delta_elect < 0: flags.append("NEGATIVE_ELECT")

    # --- ПРАВИЛО 1: Нулевое потребление ---
    if current_delta_hot == 0 and mean(deltas['hot']) > 0.1: flags.append("ZERO_HOT")
    if current_delta_cold == 0 and mean(deltas['cold']) > 0.1: flags.append("ZERO_COLD")
    if current_delta_elect == 0 and mean(deltas['elect']) > 1.0: flags.append("ZERO_ELECT")

    # --- ПРАВИЛО 2: Статистический выброс (более 2.5 стандартных отклонений) ---
    if len(history) >= 3:  # Нужно хотя бы 2 дельты
        for res_type in ['hot', 'cold', 'elect']:
            hist_deltas = deltas[res_type]
            if not hist_deltas: continue

            avg = mean(hist_deltas)
            std_dev = stdev(hist_deltas) if len(hist_deltas) > 1 else avg

            # Устанавливаем порог (нижняя граница 0.1, чтобы не реагировать на мелочи)
            threshold = avg + 2.5 * std_dev + 0.1

            current_delta = locals().get(f"current_delta_{res_type}")

            if current_delta > threshold and current_delta > 1.0:  # 1.0 - минимальный порог для аномалии
                flags.append(f"HIGH_{res_type.upper()}")

    # --- ПРАВИЛО 3: "Замерзший" счетчик (3 последних показания одинаковы) ---
    if len(history) >= 2:
        if history[0].hot_water == history[1].hot_water == current_reading.hot_water: flags.append("FROZEN_HOT")
        if history[0].cold_water == history[1].cold_water == current_reading.cold_water: flags.append("FROZEN_COLD")
        if history[0].electricity == history[1].electricity == current_reading.electricity: flags.append("FROZEN_ELECT")

    if not flags:
        return None

    # --- ПРАВИЛО 4: Сравнение со средним по группе (общежитию) ---
    if avg_peer_consumption:
        # Считаем аномалией, если расход превышает средний более чем в 3 раза (коэффициент можно настроить)
        if avg_peer_consumption.get('avg_hot', 0) > 1.0 and \
                current_delta_hot > avg_peer_consumption['avg_hot'] * 3:
            flags.append("HIGH_VS_PEERS_HOT")

        if avg_peer_consumption.get('avg_cold', 0) > 1.0 and \
                current_delta_cold > avg_peer_consumption['avg_cold'] * 3:
            flags.append("HIGH_VS_PEERS_COLD")

        if avg_peer_consumption.get('avg_elect', 0) > 10.0 and \
                current_delta_elect > avg_peer_consumption['avg_elect'] * 3:
            flags.append("HIGH_VS_PEERS_ELECT")

    if not flags:
        return None

    return ",".join(sorted(list(set(flags))))