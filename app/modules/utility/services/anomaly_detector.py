# app/modules/utility/services/anomaly_detector.py
from typing import List, Optional, Dict, Tuple
from statistics import median
from decimal import Decimal
from app.modules.utility.models import MeterReading, User


def D(val) -> Decimal:
    if val is None: return Decimal("0.000")
    return Decimal(str(val))


def mad(data: List[Decimal]) -> Decimal:
    """Median Absolute Deviation - устойчивая к выбросам статистика"""
    if not data: return Decimal("0")
    med = median(data)
    return median([abs(x - med) for x in data])


def analyze_resource(current_delta: Decimal, hist_deltas: List[Decimal], name: str,
                     avg_peer: Optional[Decimal] = None) -> Tuple[List[str], int]:
    flags = []
    score = 0

    if not hist_deltas:
        return flags, score

    med = median(hist_deltas)
    m = mad(hist_deltas) or Decimal("0.5")  # Защита от деления на 0

    # 1. SPIKE (Аномальный скачок: > Медиана + 4 MAD)
    if current_delta > med + (m * Decimal("4")) and current_delta > Decimal("1.0"):
        flags.append(f"SPIKE_{name}")
        score += 40
    # 2. SOFT SPIKE (Просто высокий)
    elif current_delta > med * Decimal("2") and current_delta > Decimal("1.0"):
        flags.append(f"HIGH_{name}")
        score += 20

    # 3. ZERO (Вдруг перестал платить)
    if current_delta == 0 and med > Decimal("1.0"):
        flags.append(f"ZERO_{name}")
        score += 25

    # 4. FROZEN (Замерзший счетчик)
    if len(hist_deltas) >= 3 and all(d == 0 for d in hist_deltas[-3:]) and current_delta == 0:
        flags.append(f"FROZEN_{name}")
        score += 30

    # 5. FLAT (Рисует одни и те же цифры потребления, например ровно по 3 куба каждый месяц)
    if len(hist_deltas) >= 3 and len(set(hist_deltas[-3:] + [current_delta])) == 1 and current_delta > 0:
        flags.append(f"FLAT_{name}")
        score += 35

    # 6. TREND UP (Скрытая утечка - рост 3 месяца подряд)
    if len(hist_deltas) >= 3:
        if hist_deltas[-3] < hist_deltas[-2] < hist_deltas[-1] < current_delta:
            flags.append(f"TREND_UP_{name}")
            score += 30

    # 7. DROP AFTER SPIKE (Анти-читинг: сброс показаний после проверки)
    if len(hist_deltas) >= 2:
        if hist_deltas[-1] > med * Decimal("3") and current_delta < med * Decimal("0.3"):
            flags.append(f"DROP_AFTER_SPIKE_{name}")
            score += 40

    # 8. Сравнение с группой
    if avg_peer and avg_peer > 0:
        if current_delta > avg_peer * Decimal("3"):
            flags.append(f"HIGH_VS_PEERS_{name}")
            score += 20

    return flags, score


def check_reading_for_anomalies_v2(
        current_reading: MeterReading,
        history: List[MeterReading],
        user: Optional[User] = None,
        avg_peer_consumption: Optional[Dict[str, Decimal]] = None
) -> Tuple[Optional[str], int]:
    """
    Возвращает: (строка_флагов, risk_score)
    """
    if not history or len(history) < 2:
        return None, 0

    flags = []
    total_score = 0
    last = history[0]

    current_deltas = {
        "HOT": D(current_reading.hot_water) - D(last.hot_water),
        "COLD": D(current_reading.cold_water) - D(last.cold_water),
        "ELECT": D(current_reading.electricity) - D(last.electricity),
    }

    # --- 1. КРИТИЧЕСКИЕ ПРОВЕРКИ ---
    for k, v in current_deltas.items():
        if v < 0:
            flags.append(f"NEGATIVE_{k}")
            total_score += 100  # Максимальный риск

    # --- Подготовка истории (нужна в хронологическом порядке: от старых к новым) ---
    # history отсортирован DESC (0 - самый новый)
    hist_deltas = {"HOT": [], "COLD": [], "ELECT": []}

    # Идем с конца истории к началу
    for i in range(len(history) - 1, 0, -1):
        prev = history[i]
        curr = history[i - 1]
        hist_deltas["HOT"].append(max(Decimal(0), D(curr.hot_water) - D(prev.hot_water)))
        hist_deltas["COLD"].append(max(Decimal(0), D(curr.cold_water) - D(prev.cold_water)))
        hist_deltas["ELECT"].append(max(Decimal(0), D(curr.electricity) - D(prev.electricity)))

    # --- 2. СТАТИСТИЧЕСКИЙ И ПОВЕДЕНЧЕСКИЙ АНАЛИЗ ---
    for key in ["HOT", "COLD", "ELECT"]:
        peer_avg = avg_peer_consumption.get(f"avg_{key.lower()}") if avg_peer_consumption else None

        f, s = analyze_resource(current_deltas[key], hist_deltas[key], key, peer_avg)
        flags.extend(f)
        total_score += s

    # --- 3. КОНТЕКСТНЫЙ АНАЛИЗ (ПО ЖИЛЬЦАМ) ---
    if user and getattr(user, 'residents_count', 1) > 0:
        per_person = current_deltas["COLD"] / Decimal(str(user.residents_count))
        # Больше 12 кубов холодной воды на 1 человека в месяц - сильная аномалия
        if per_person > Decimal("12.0"):
            flags.append("HIGH_PER_PERSON_COLD")
            total_score += 25

    # Капим счетчик на 100
    total_score = min(total_score, 100)

    if not flags:
        return None, 0

    return ",".join(sorted(list(set(flags)))), total_score