"""Единая точка расчёта стоимости одного MeterReading.

Используется ДВУМЯ путями:
  - gsheets_sync.promote_auto_approved_rows — при auto-approve подачи из
    Google Sheets, чтобы сразу проставлять реальные суммы (а не 0!).
  - app.scripts.recalc_zero_gsheets_readings — для пересчёта исторически
    «забытых» reading'ов где cost_* = 0 и total_cost = 0.

Контракт: на входе reading и tariff (опционально prev_reading), на выходе
dict со всеми cost_* + total_209/205/cost. Caller сохраняет в БД.

История появления (may 2026): до этого helper'а promote сохранял
MeterReading с total_cost=0, не вызывая calculate_utilities. Жилец
видел «нулевую квитанцию» при реальной подаче — в админке flag
GSHEETS_AUTO + total = 0 ₽, в PDF все умножения тариф×объём = 0.00.
Деньги физически не начислялись.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.modules.utility.models import MeterReading, Room, Tariff, User
from app.modules.utility.services.calculations import (
    CalculationError,
    calculate_utilities,
)


ZERO = Decimal("0.00")


def _to_dec(value) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def compute_reading_breakdown(
    *,
    user: User,
    room: Room,
    tariff: Tariff,
    current_hot: Decimal,
    current_cold: Decimal,
    current_elect: Decimal,
    prev_reading: Optional[MeterReading] = None,
) -> dict:
    """Считает breakdown стоимости для reading и возвращает все поля,
    которые caller должен записать в MeterReading.

    Аргументы:
      current_hot/cold/elect — текущие показания счётчиков (накопленные).
      prev_reading           — предыдущая утверждённая подача жильца в этой
                               комнате; None означает baseline (первая подача,
                               расход не известен — возвращаем все нули).

    Возвращает dict со ключами:
      cost_hot_water, cost_cold_water, cost_sewage, cost_electricity,
      cost_maintenance, cost_social_rent, cost_waste, cost_fixed_part
      — компоненты для setattr на MeterReading;
      total_cost, total_209, total_205 — итоги для записи в БД;
      sanity_warning — необязательное предупреждение от calculate_utilities
                       (передавать вверх для UI/логов).

    Raises CalculationError — если тариф пустой (см. calculate_utilities).
    """
    # Baseline: первая подача жильца — счёт = 0, начислять нечего.
    # Это согласовано с client_readings/admin_readings_approve: в обоих
    # местах при prev=None вся стоимость = 0, чтобы не считать дельту от
    # «грязных» прошлых жильцов.
    if prev_reading is None:
        return {
            "cost_hot_water": ZERO, "cost_cold_water": ZERO,
            "cost_sewage": ZERO, "cost_electricity": ZERO,
            "cost_maintenance": ZERO, "cost_social_rent": ZERO,
            "cost_waste": ZERO, "cost_fixed_part": ZERO,
            "total_cost": ZERO,
            "total_209": ZERO, "total_205": ZERO,
            "sanity_warning": None,
            "is_baseline": True,
        }

    p_hot = _to_dec(prev_reading.hot_water)
    p_cold = _to_dec(prev_reading.cold_water)
    p_elect = _to_dec(prev_reading.electricity)

    cur_hot = _to_dec(current_hot)
    cur_cold = _to_dec(current_cold)
    cur_elect = _to_dec(current_elect)

    # Дельты с защитой от отрицательных (счётчик не должен уменьшаться,
    # но на исторических данных бывает — не падаем, просто ставим 0).
    d_hot = max(ZERO, cur_hot - p_hot)
    d_cold = max(ZERO, cur_cold - p_cold)
    d_elect = max(ZERO, cur_elect - p_elect)

    # Доля жильца в комнатном расходе электричества (как в client_readings).
    residents = Decimal(user.residents_count or 1)
    total_room = Decimal(room.total_room_residents or 1)
    if total_room <= 0:
        total_room = Decimal("1")
    elect_share = (residents / total_room) * d_elect

    # Расчёт. Может бросить CalculationError если тариф полностью пуст —
    # пропагандируем наверх, caller решит что делать (логировать и пропустить).
    costs = calculate_utilities(
        user=user, room=room, tariff=tariff,
        volume_hot=d_hot, volume_cold=d_cold,
        volume_sewage=d_hot + d_cold,
        volume_electricity_share=elect_share,
    )

    # Декомпозиция total → 209/205. Та же логика что в client_readings.py:
    # 205 = социальный найм; 209 = всё остальное (коммуналка + содержание +
    # отопление + ТКО). У этого reading нет долгов/корректировок (gsheets
    # только что создал) — их учитывает client_readings/admin_approve когда
    # подача проходит через ручной flow.
    cost_rent = costs["cost_social_rent"]
    total_205 = cost_rent
    total_209 = costs["total_cost"] - cost_rent
    total_cost = costs["total_cost"]

    return {
        "cost_hot_water":   costs["cost_hot_water"],
        "cost_cold_water":  costs["cost_cold_water"],
        "cost_sewage":      costs["cost_sewage"],
        "cost_electricity": costs["cost_electricity"],
        "cost_maintenance": costs["cost_maintenance"],
        "cost_social_rent": costs["cost_social_rent"],
        "cost_waste":       costs["cost_waste"],
        "cost_fixed_part":  costs["cost_fixed_part"],
        "total_cost":       total_cost,
        "total_209":        total_209,
        "total_205":        total_205,
        "sanity_warning":   costs.get("sanity_warning"),
        "is_baseline":      False,
    }


__all__ = ["compute_reading_breakdown", "CalculationError"]
