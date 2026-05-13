# app/modules/utility/services/anomaly_detector.py
"""
Anomaly Detector v3 — теперь с конфигом и self-learning.

Изменения относительно v2:
1. Все пороги читаются из `analyzer_config` (таблица analyzer_settings),
   а не захардкожены. Админ меняет в UI «Центр анализа» — без релиза.
2. Учитывается `anomaly_dismissals`: если админ пометил флаг для жильца
   как «не аномалия» — этот флаг больше не выставляется (false-positive
   не повторяется). Глобальные dismissals (user_id=NULL) отключают
   правило для всех.
3. Добавлены 4 новых правила:
     ROUND_NUMBER_X — подозрение на округление (целое без дробей).
     HOT_GT_COLD    — ГВС > ХВС, физически нетипично.
     COPY_NEIGHBOR  — значения совпадают с соседом по комнате с точностью
                      до epsilon — подозрение на «списали друг у друга».
     GAP_RECOVERY   — большая подача после долгой паузы (3+ месяца без подач).

Каждое новое правило управляется флагом rule.<name>.enabled в конфиге.
"""
from __future__ import annotations

from decimal import Decimal
from statistics import median
from typing import Dict, List, Optional, Tuple

from app.modules.utility.models import MeterReading, User
from app.modules.utility.services.analyzer_config import config, dismissals


def D(val) -> Decimal:
    if val is None:
        return Decimal("0.000")
    return Decimal(str(val))


# Точное соответствие «флаг → сколько score добавляет». Раньше self-learning
# вычитал фиксированные 20 score за КАЖДЫЙ dismissed-флаг — это неточно
# (флаги дают от 10 до 100 score). Теперь — точная коррекция.
def _flag_score(flag: str) -> int:
    """Возвращает score, который данный флаг прибавил к total_score.

    Поддерживает префиксные имена (SPIKE_HOT → 40 как у любого SPIKE_*)
    и точные совпадения для уникальных флагов (HOT_GT_COLD, GAP_RECOVERY).
    """
    if flag.startswith("NEGATIVE_"):           return 100
    if flag.startswith("DROP_AFTER_SPIKE_"):   return 40
    if flag.startswith("SPIKE_"):              return 40
    if flag.startswith("FLAT_"):               return 35
    if flag.startswith("FROZEN_"):             return 30
    if flag.startswith("TREND_UP_"):           return 30
    if flag.startswith("ZERO_"):               return 25
    if flag.startswith("HIGH_VS_PEERS_"):      return 20
    if flag.startswith("HIGH_PER_PERSON_"):    return 25
    if flag.startswith("HIGH_"):               return 20
    if flag.startswith("ROUND_NUMBER_"):       return 10
    if flag == "HOT_GT_COLD":                  return 30
    if flag == "COPY_NEIGHBOR":                return 35
    if flag == "COPY_NEIGHBOR_PARTIAL":        return 15
    if flag == "GAP_RECOVERY":                 return 25
    if flag == "COMBO_SUSPICIOUS":             return 25
    return 20  # fallback для будущих флагов


# Список «подозрительных» флагов, комбинации которых дают доп. бонус
# (см. COMBO_SUSPICIOUS ниже). Это флаги где КАЖДЫЙ сам по себе мягкий,
# но 3+ одновременно — почти наверняка подделка.
_SUSPICIOUS_PREFIXES = (
    "FLAT_", "ROUND_NUMBER_", "DROP_AFTER_SPIKE_", "ZERO_", "FROZEN_",
)
_SUSPICIOUS_EXACT = ("COPY_NEIGHBOR", "COPY_NEIGHBOR_PARTIAL", "HOT_GT_COLD")


def mad(data: List[Decimal]) -> Decimal:
    """Median Absolute Deviation — устойчивая к выбросам статистика."""
    if not data:
        return Decimal("0")
    med = median(data)
    return median([abs(x - med) for x in data])


# ---------------------------------------------------------------------------
# СТАТИСТИЧЕСКИЕ И ПОВЕДЕНЧЕСКИЕ ПРАВИЛА (v2 + конфиг)
# ---------------------------------------------------------------------------
def analyze_resource(
    current_delta: Decimal,
    hist_deltas: List[Decimal],
    name: str,
    avg_peer: Optional[Decimal] = None,
    meter_present: bool = True,
) -> Tuple[List[str], int]:
    flags: list[str] = []
    score = 0

    # Если у жильца нет соответствующего счётчика (has_X_meter=False) —
    # никаких аномалий по этому ресурсу не считаем. Подача 0 = ожидаемое
    # поведение, исторические дельты могут быть произвольными (если счётчик
    # был, потом снят). См. миграцию meters_001_per_user_config.
    if not meter_present:
        return flags, score

    if not hist_deltas:
        return flags, score

    med = median(hist_deltas)
    m = mad(hist_deltas) or Decimal("0.5")

    mad_mult = Decimal(str(config.get_int("anomaly.mad_multiplier", 4)))
    soft_factor = Decimal(str(config.get_float("anomaly.soft_spike_factor", 2.0)))
    peer_factor = Decimal(str(config.get_float("anomaly.peer_factor", 3.0)))

    # 1. SPIKE: Аномальный скачок > Median + N×MAD
    if current_delta > med + m * mad_mult and current_delta > Decimal("1.0"):
        flags.append(f"SPIKE_{name}")
        score += 40
    # 2. SOFT SPIKE
    elif current_delta > med * soft_factor and current_delta > Decimal("1.0"):
        flags.append(f"HIGH_{name}")
        score += 20

    # 3. ZERO
    if current_delta == 0 and med > Decimal("1.0"):
        flags.append(f"ZERO_{name}")
        score += 25

    # 4. FROZEN
    if (
        len(hist_deltas) >= 3
        and all(d == 0 for d in hist_deltas[-3:])
        and current_delta == 0
    ):
        flags.append(f"FROZEN_{name}")
        score += 30

    # 5. FLAT — ровно одно и то же N раз подряд
    if (
        len(hist_deltas) >= 3
        and len(set(hist_deltas[-3:] + [current_delta])) == 1
        and current_delta > 0
    ):
        flags.append(f"FLAT_{name}")
        score += 35

    # 6. TREND_UP — скрытая утечка
    if len(hist_deltas) >= 3:
        if hist_deltas[-3] < hist_deltas[-2] < hist_deltas[-1] < current_delta:
            flags.append(f"TREND_UP_{name}")
            score += 30

    # 7. DROP_AFTER_SPIKE — анти-чит сброс после высокой подачи
    if len(hist_deltas) >= 2:
        # Раньше захардкожено 3.0 / 0.3 — теперь через config, чтобы админ
        # мог тюнить чувствительность для конкретного общежития.
        high_factor = Decimal(str(config.get_float("anomaly.drop_after_spike.high_factor", 3.0)))
        low_factor = Decimal(str(config.get_float("anomaly.drop_after_spike.low_factor", 0.3)))
        if (
            hist_deltas[-1] > med * high_factor
            and current_delta < med * low_factor
        ):
            flags.append(f"DROP_AFTER_SPIKE_{name}")
            score += 40

    # 8. HIGH_VS_PEERS
    if avg_peer and avg_peer > 0:
        if current_delta > avg_peer * peer_factor:
            flags.append(f"HIGH_VS_PEERS_{name}")
            score += 20

    return flags, score


# ---------------------------------------------------------------------------
# НОВЫЕ ПРАВИЛА (v3) — каждое under-control через rule.<name>.enabled
# ---------------------------------------------------------------------------
def _check_round_number(current_deltas: Dict[str, Decimal]) -> Tuple[List[str], int]:
    """Подозрение на округление: целое число без дробной части и delta >= порога.
    Реальный счётчик никогда не показывает ровные числа — это всегда «на глаз».
    Малые значения пропускаем — слишком много ложных срабатываний."""
    if not config.is_rule_enabled("rule.round_number"):
        return [], 0
    min_value = Decimal(str(config.get_float("anomaly.round_number.min_value", 2.0)))
    flags: list[str] = []
    for name, delta in current_deltas.items():
        if delta >= min_value and delta == delta.to_integral_value():
            flags.append(f"ROUND_NUMBER_{name}")
    return flags, 10 * len(flags)  # +10 за каждый — мягкий сигнал, не критично


def _check_hot_gt_cold(current_deltas: Dict[str, Decimal]) -> Tuple[List[str], int]:
    """ГВС > ХВС за период — физически странно. Холодная вода идёт не только
    в краны (питьё, готовка, стирка), но и в подогреватель. Поэтому ХВС обычно
    >= ГВС. Если наоборот — счётчик перепутан местами либо подделка."""
    if not config.is_rule_enabled("rule.hot_gt_cold"):
        return [], 0
    hot = current_deltas.get("HOT", Decimal("0"))
    cold = current_deltas.get("COLD", Decimal("0"))
    # Допускаем равенство — бывает у одиночек с одной точкой водозабора.
    factor = Decimal(str(config.get_float("anomaly.hot_gt_cold.factor", 1.2)))
    if cold > 0 and hot > cold * factor:
        return ["HOT_GT_COLD"], 30
    return [], 0


def _check_gap_recovery(
    current_reading: MeterReading,
    history: List[MeterReading],
    current_deltas: Dict[str, Decimal],
) -> Tuple[List[str], int]:
    """Если жилец не подавал N+ месяцев, а затем сразу пришёл с большой подачей —
    подозрительно. Может быть честным накопленным расходом (был в отъезде, потом
    включил), а может — попыткой перекинуть высокий расход на «спокойный» период."""
    if not config.is_rule_enabled("rule.gap_recovery"):
        return [], 0
    if not history:
        return [], 0
    last = history[0]
    if not last.created_at or not current_reading.created_at:
        return [], 0
    gap_days = (current_reading.created_at - last.created_at).days
    min_days = config.get_int("anomaly.gap_recovery.min_days", 90)
    if gap_days < min_days:
        return [], 0
    # Порог «накопленного» расхода — любой ресурс с дельтой больше этого
    # значения считается «большой подачей» в gap-recovery.
    min_volume = Decimal(str(config.get_float("anomaly.gap_recovery.min_volume", 8.0)))
    big_resources = [k for k, v in current_deltas.items() if v >= min_volume]
    if big_resources:
        return ["GAP_RECOVERY"], 25
    return [], 0


def _check_copy_neighbor(
    current_reading: MeterReading,
    current_deltas: Dict[str, Decimal],
    neighbor_deltas: Optional[List[Dict[str, Decimal]]],
) -> Tuple[List[str], int]:
    """Подозрение что списали показания у соседа: дельты совпадают с одним
    из соседей по комнате с точностью до epsilon. Даже один совпавший
    ресурс — подозрительно (счётчики у разных людей расходятся почти всегда)."""
    if not config.is_rule_enabled("rule.copy_neighbor"):
        return [], 0
    if not neighbor_deltas:
        return [], 0
    eps = Decimal(str(config.get_float("rule.copy_neighbor.epsilon", 0.001)))
    flags: list[str] = []
    for nb in neighbor_deltas:
        matches = 0
        for k, v in current_deltas.items():
            nv = nb.get(k)
            if nv is None or v == 0:
                continue
            if abs(v - nv) <= eps:
                matches += 1
        if matches >= 2:
            # Совпало по 2+ ресурсам — почти точно списано.
            flags.append("COPY_NEIGHBOR")
            return flags, 35
        elif matches == 1 and len(current_deltas) >= 2:
            # Один в один по одному ресурсу — может быть совпадением, флагуем мягко.
            flags.append("COPY_NEIGHBOR_PARTIAL")
            return flags, 15
    return flags, 0


# ---------------------------------------------------------------------------
# ОСНОВНАЯ ФУНКЦИЯ
# ---------------------------------------------------------------------------
def check_reading_for_anomalies_v2(
    current_reading: MeterReading,
    history: List[MeterReading],
    user: Optional[User] = None,
    avg_peer_consumption: Optional[Dict[str, Decimal]] = None,
    neighbor_deltas: Optional[List[Dict[str, Decimal]]] = None,
) -> Tuple[Optional[str], int]:
    """Возвращает: (строка_флагов | None, risk_score 0..100).

    Параметры:
        current_reading — текущая подача (ещё не сохранённая).
        history — список MeterReading этого жильца, отсортирован DESC (0=новейший).
        user — для контекстного анализа (residents_count).
        avg_peer_consumption — словарь avg_hot/avg_cold/avg_elect среднего по группе.
        neighbor_deltas — НОВОЕ в v3: список deltas соседей по комнате за тот же период,
            используется для COPY_NEIGHBOR. Передавать необязательно.
    """
    if not history or len(history) < 2:
        return None, 0

    flags: list[str] = []
    total_score = 0
    last = history[0]

    current_deltas = {
        "HOT": D(current_reading.hot_water) - D(last.hot_water),
        "COLD": D(current_reading.cold_water) - D(last.cold_water),
        "ELECT": D(current_reading.electricity) - D(last.electricity),
    }

    # Маппинг ресурс → has_X_meter. Если у жильца нет счётчика — не флагим
    # ничего по этому ресурсу (см. меняла meters_001_per_user_config).
    meter_present_map = {
        "HOT": bool(getattr(user, "has_hw_meter", True)) if user else True,
        "COLD": bool(getattr(user, "has_cw_meter", True)) if user else True,
        "ELECT": bool(getattr(user, "has_el_meter", True)) if user else True,
    }

    # --- 1. КРИТИЧЕСКИЕ ПРОВЕРКИ ---
    for k, v in current_deltas.items():
        if not meter_present_map[k]:
            continue
        if v < 0:
            flags.append(f"NEGATIVE_{k}")
            total_score += 100

    # --- Подготовка истории (хронологически: старые → новые) ---
    hist_deltas = {"HOT": [], "COLD": [], "ELECT": []}
    for i in range(len(history) - 1, 0, -1):
        prev = history[i]
        curr = history[i - 1]
        hist_deltas["HOT"].append(max(Decimal(0), D(curr.hot_water) - D(prev.hot_water)))
        hist_deltas["COLD"].append(max(Decimal(0), D(curr.cold_water) - D(prev.cold_water)))
        hist_deltas["ELECT"].append(max(Decimal(0), D(curr.electricity) - D(prev.electricity)))

    # --- 2. СТАТИСТИЧЕСКИЙ И ПОВЕДЕНЧЕСКИЙ АНАЛИЗ ---
    for key in ["HOT", "COLD", "ELECT"]:
        peer_avg = (
            avg_peer_consumption.get(f"avg_{key.lower()}")
            if avg_peer_consumption else None
        )
        f, s = analyze_resource(
            current_deltas[key],
            hist_deltas[key],
            key,
            peer_avg,
            meter_present=meter_present_map[key],
        )
        flags.extend(f)
        total_score += s

    # --- 3. КОНТЕКСТНЫЙ АНАЛИЗ ---
    if user and getattr(user, "residents_count", 1) > 0:
        rc = Decimal(str(user.residents_count))
        # COLD per person (исторически было только это) —
        # пропускаем для жильцов без счётчика ХВС.
        if meter_present_map["COLD"]:
            per_person_cold_limit = Decimal(str(
                config.get_float("anomaly.high_per_person_cold", 12.0)
            ))
            per_person_cold = current_deltas["COLD"] / rc
            if per_person_cold > per_person_cold_limit:
                flags.append("HIGH_PER_PERSON_COLD")
                total_score += 25
        # ELECT per person — симметрия (раньше была только для воды).
        # 200 кВт·ч/чел/мес — реалистичный потолок жилого потребления;
        # выше — серверная ферма, грязный счётчик или ошибка ввода.
        if meter_present_map["ELECT"]:
            per_person_elect_limit = Decimal(str(
                config.get_float("anomaly.high_per_person_elect", 200.0)
            ))
            per_person_elect = current_deltas["ELECT"] / rc
            if per_person_elect > per_person_elect_limit:
                flags.append("HIGH_PER_PERSON_ELECT")
                total_score += 25

    # --- 4. НОВЫЕ ПРАВИЛА v3 ---
    for rule_fn, args in (
        (_check_round_number, (current_deltas,)),
        (_check_hot_gt_cold, (current_deltas,)),
        (_check_gap_recovery, (current_reading, history, current_deltas)),
        (_check_copy_neighbor, (current_reading, current_deltas, neighbor_deltas)),
    ):
        f, s = rule_fn(*args)
        flags.extend(f)
        total_score += s

    # --- 5. SELF-LEARNING: убираем dismissed-флаги ---
    # Раньше отнимали +20 score за каждый dismissed-флаг — но реальные
    # флаги дают от 10 (ROUND_NUMBER) до 100 (NEGATIVE), это давало
    # неточный score. Теперь _flag_score возвращает точную «цену»
    # каждого флага, и при dismissal вычитаем именно её.
    user_id = getattr(current_reading, "user_id", None) or getattr(user, "id", None)
    filtered_flags: list[str] = []
    removed_score = 0
    for flag in flags:
        if dismissals.is_dismissed(user_id, flag):
            removed_score += _flag_score(flag)
        else:
            filtered_flags.append(flag)
    flags = filtered_flags
    total_score = max(0, total_score - removed_score)

    # --- 6. COMBO BONUS: 3+ «подозрительных» флагов одновременно ---
    # Каждый flag сам по себе мягкий (FLAT, ROUND_NUMBER, COPY_NEIGHBOR_PARTIAL)
    # — обычный человек может случайно совпасть один-два раза. Но три и
    # больше за один период — почти точно подделка. Добавляем COMBO_SUSPICIOUS
    # как отдельный флаг для UI и +25 к score.
    suspicious_count = sum(
        1 for f in flags
        if f in _SUSPICIOUS_EXACT
        or any(f.startswith(p) for p in _SUSPICIOUS_PREFIXES)
    )
    if suspicious_count >= 3:
        flags.append("COMBO_SUSPICIOUS")
        total_score += 25

    total_score = min(total_score, 100)

    if not flags:
        return None, 0

    return ",".join(sorted(set(flags))), total_score
