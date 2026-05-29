"""Пересчёт MeterReading'ов холостяцких квартир по НОВОЙ формуле (29.05.2026).

Контекст: формула начислений для холостяков изменилась.
  Было:  все статьи делились на total_room_residents (наём = area × rate ÷ N).
  Стало: area-based (наём/ТКО/отопление/содержание) = (area / max_capacity) × rate,
         каждому жильцу полностью; счётчики ÷ факт. число жильцов; электричество
         не делится повторно. ОДН удалён.

Reading'и, созданные/утверждённые ДО деплоя новой формулы (например авто-заполнение
Май 2026), посчитаны по старой формуле — наём завышен. Этот скрипт пересчитывает
их через единый helper compute_reading_breakdown (та же формула, что и для новых).

Берёт ТОЛЬКО approved-reading'и холостяцких комнат (room.is_singles_apartment=true).
Семейные не трогает. Меняет cost_* и total_209/total_205, СОХРАНЯЯ перенесённые
долги/корректировки (carried balance = total_* − чистые начисления). total_cost
пересчитает триггер trg_readings_sync_total_cost из 209+205 — руками не ставим
(см. memory/total_cost_trigger_gotcha).

ВАЖНО: dry-run по умолчанию. Для применения — флаг --apply (после backup).

Использование:

    # Превью (НИЧЕГО не меняет):
    docker exec utility_calc_web_jkh python -m app.scripts.recalc_singles_formula

    # Только активный период (или конкретный):
    docker exec utility_calc_web_jkh python -m app.scripts.recalc_singles_formula --period-id 88

    # Применить (после backup):
    docker exec utility_calc_backup /usr/local/bin/backup.sh
    docker exec utility_calc_web_jkh python -m app.scripts.recalc_singles_formula --period-id 88 --apply
"""
from __future__ import annotations

import asyncio
from argparse import ArgumentParser
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.modules.utility.models import (
    MeterReading,
    Room,
    Tariff,
    User,
)
from app.modules.utility.services.calculations import CalculationError
from app.modules.utility.services.reading_calculator import (
    compute_reading_breakdown,
)
from app.modules.utility.services.tariff_cache import tariff_cache


ZERO = Decimal("0.00")


def _dec(value) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


async def find_targets(db, period_id: Optional[int]) -> list[MeterReading]:
    """approved-reading'и холостяцких комнат (room.is_singles_apartment=true)."""
    stmt = (
        select(MeterReading)
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
            selectinload(MeterReading.period),
        )
        .join(Room, MeterReading.room_id == Room.id)
        .where(
            MeterReading.is_approved.is_(True),
            Room.is_singles_apartment.is_(True),
        )
    )
    if period_id is not None:
        stmt = stmt.where(MeterReading.period_id == period_id)
    return list((await db.execute(stmt)).scalars().all())


async def find_prev(db, reading: MeterReading) -> Optional[MeterReading]:
    """Предыдущий approved-reading жильца в той же комнате (по period_id,
    не по created_at — см. инцидент may 2026). total>0, чтобы не цепляться
    за сломанные нулевые."""
    if reading.period_id is None:
        return None
    res = await db.execute(
        select(MeterReading)
        .where(
            MeterReading.user_id == reading.user_id,
            MeterReading.room_id == reading.room_id,
            MeterReading.is_approved.is_(True),
            MeterReading.period_id < reading.period_id,
            MeterReading.total_cost > ZERO,
        )
        .order_by(MeterReading.period_id.desc())
        .limit(1)
    )
    return res.scalars().first()


def _pure_205(r: MeterReading) -> Decimal:
    """Чистое начисление по 205 счёту = наём."""
    return _dec(r.cost_social_rent)


def _pure_209(r: MeterReading) -> Decimal:
    """Чистое начисление по 209 = всё кроме наёма."""
    return (
        _dec(r.cost_hot_water) + _dec(r.cost_cold_water) + _dec(r.cost_sewage)
        + _dec(r.cost_electricity) + _dec(r.cost_maintenance)
        + _dec(r.cost_waste) + _dec(r.cost_fixed_part)
    )


async def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Применить (по умолчанию — DRY-RUN).")
    parser.add_argument("--period-id", type=int, default=None,
                        help="Ограничить периодом. По умолчанию — все.")
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        targets = await find_targets(db, args.period_id)
        print(f"Найдено approved-reading'ов холостяцких квартир: {len(targets)}")
        if args.period_id is not None:
            print(f"(фильтр period_id={args.period_id})")
        print()
        if not targets:
            print("OK — нечего пересчитывать.")
            return 0

        from app.modules.utility.routers.settings import _load_seasonal
        _seasonal = await _load_seasonal(db)

        # (reading, breakdown|None, carried_209, carried_205, error|None)
        results: list[tuple] = []
        for r in targets:
            user = r.user
            room = user.room if user else None
            if not user or not room:
                results.append((r, None, ZERO, ZERO, "no user/room"))
                continue
            tariff = tariff_cache.get_effective_tariff(user=user, room=room)
            if not tariff:
                tariff = (await db.execute(
                    select(Tariff).where(Tariff.is_active.is_(True))
                )).scalars().first()
            if not tariff:
                results.append((r, None, ZERO, ZERO, "no active tariff"))
                continue

            # Перенесённые долги/корректировки = total_* − чистые начисления.
            # Сохраняем их, чтобы пересчёт не стёр задолженность из прошлых периодов.
            carried_209 = _dec(r.total_209) - _pure_209(r)
            carried_205 = _dec(r.total_205) - _pure_205(r)

            prev = await find_prev(db, r)
            _heating = _seasonal.heating_season_active and tariff.is_heating_active_now()
            _hw = _seasonal.hot_water_heating_active and tariff.is_hw_heating_active_now()
            try:
                b = compute_reading_breakdown(
                    user=user, room=room, tariff=tariff,
                    current_hot=r.hot_water or 0,
                    current_cold=r.cold_water or 0,
                    current_elect=r.electricity or 0,
                    prev_reading=prev,
                    heating_season_active=_heating,
                    hot_water_heating_active=_hw,
                )
                results.append((r, b, carried_209, carried_205, None))
            except CalculationError as e:
                results.append((r, None, ZERO, ZERO, f"calc_error: {e}"))

        ok = [x for x in results if x[1] is not None]
        bad = [x for x in results if x[1] is None]

        print(f"{'id':>6} {'user':>5} {'комната':<18} {'наём old':>10} {'наём new':>10} "
              f"{'total old':>11} {'total new':>11}")
        for r, b, c209, c205, _ in sorted(ok, key=lambda x: x[0].user_id):
            old_rent = float(_pure_205(r))
            new_rent = float(b["cost_social_rent"])
            old_total = float(_dec(r.total_cost))
            # новый total_cost = (209_pure+carried) + (205_pure+carried)
            new_total = float(
                (b["total_cost"] - b["cost_social_rent"] + c209)
                + (b["cost_social_rent"] + c205)
            )
            room = r.user.room
            label = f"{room.dormitory_name} {room.room_number}" if room else f"#{r.room_id}"
            print(f"{r.id:>6} {r.user_id:>5} {label:<18} {old_rent:>10.2f} {new_rent:>10.2f} "
                  f"{old_total:>11.2f} {new_total:>11.2f}")

        if bad:
            print()
            from collections import Counter
            print(f"Не удалось пересчитать ({len(bad)}):")
            for reason, cnt in Counter(x[4] for x in bad).most_common():
                print(f"  {reason}: {cnt}")

        if not args.apply:
            print()
            print("=" * 78)
            print("DRY-RUN. Изменений не сделано. Для применения: --apply (после backup).")
            print("=" * 78)
            return 0

        print()
        print("=" * 78)
        print(f"APPLY: пересчитываю {len(ok)} reading'ов ...")
        for r, b, c209, c205, _ in ok:
            # carried сохраняем; total_cost пересчитает триггер из 209+205.
            r.total_209 = b["total_cost"] - b["cost_social_rent"] + c209
            r.total_205 = b["cost_social_rent"] + c205
            r.cost_hot_water = b["cost_hot_water"]
            r.cost_cold_water = b["cost_cold_water"]
            r.cost_sewage = b["cost_sewage"]
            r.cost_electricity = b["cost_electricity"]
            r.cost_maintenance = b["cost_maintenance"]
            r.cost_social_rent = b["cost_social_rent"]
            r.cost_waste = b["cost_waste"]
            r.cost_fixed_part = b["cost_fixed_part"]
            db.add(r)
        await db.commit()
        print(f"Готово. {len(ok)} reading'ов пересчитаны.")
        print("=" * 78)
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
