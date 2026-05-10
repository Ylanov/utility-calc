"""Пересчёт MeterReading с anomaly_flags='GSHEETS_AUTO' и total_cost=0.

Контекст: до фикса в gsheets_sync.promote_auto_approved_rows (may 2026)
auto-approve gsheets-подач сохранял MeterReading с total_cost=0,
не вызывая calculate_utilities. Жилец видел «нулевую квитанцию» при
реальной подаче (всё × тариф = 0.00 на PDF), деньги не начислялись.

Этот скрипт находит такие reading'и, подтягивает их prev_reading
(того же жильца в той же комнате, ближайший предыдущий approved),
тариф из tariff_cache, и пересчитывает через единый helper
compute_reading_breakdown — тот же, что теперь использует фикснутый
promote. Гарантия: пересчёт делается ТЕМИ ЖЕ формулами что и для
новых reading'ов.

ВАЖНО: dry-run по умолчанию. Для реального применения — флаг --apply.
Делает UPDATE (не DELETE и не пересоздание). Меняет:
  total_cost, total_209, total_205, cost_hot_water, cost_cold_water,
  cost_sewage, cost_electricity, cost_maintenance, cost_social_rent,
  cost_waste, cost_fixed_part.
Не трогает hot_water/cold_water/electricity (показания счётчиков
остаются как в исходной gsheets-подаче).

Использование:

    # Посмотреть что будет пересчитано (НИЧЕГО не меняет):
    docker exec utility_calc_web_jkh python -m app.scripts.recalc_zero_gsheets_readings

    # Применить (после backup):
    docker exec utility_calc_backup /usr/local/bin/backup.sh
    docker exec utility_calc_web_jkh python -m app.scripts.recalc_zero_gsheets_readings --apply

    # Только для конкретного периода:
    docker exec utility_calc_web_jkh python -m app.scripts.recalc_zero_gsheets_readings --period-id 2 --apply
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
    BillingPeriod,
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


async def find_targets(db, period_id: Optional[int]) -> list[MeterReading]:
    stmt = (
        select(MeterReading)
        .options(
            selectinload(MeterReading.user).selectinload(User.room),
            selectinload(MeterReading.period),
        )
        .where(
            MeterReading.is_approved.is_(True),
            MeterReading.total_cost == ZERO,
            MeterReading.anomaly_flags.like("GSHEETS_AUTO%"),
        )
    )
    if period_id is not None:
        stmt = stmt.where(MeterReading.period_id == period_id)
    return list((await db.execute(stmt)).scalars().all())


async def find_prev(db, reading: MeterReading) -> Optional[MeterReading]:
    """Находит предыдущее utility-approved-reading жильца в той же комнате."""
    res = await db.execute(
        select(MeterReading)
        .where(
            MeterReading.user_id == reading.user_id,
            MeterReading.room_id == reading.room_id,
            MeterReading.is_approved.is_(True),
            MeterReading.created_at < reading.created_at,
            MeterReading.total_cost > ZERO,  # не берём другие zero-broken
        )
        .order_by(MeterReading.created_at.desc())
        .limit(1)
    )
    return res.scalars().first()


async def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Применить (по умолчанию — DRY-RUN).",
    )
    parser.add_argument(
        "--period-id",
        type=int,
        default=None,
        help="Ограничить конкретным period_id. По умолчанию — все периоды.",
    )
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        targets = await find_targets(db, args.period_id)
        print(f"Найдено GSHEETS_AUTO-readings с total_cost=0: {len(targets)}")
        if args.period_id is not None:
            print(f"(фильтр period_id={args.period_id})")
        print()

        if not targets:
            print("OK — нечего пересчитывать.")
            return 0

        recalc_results: list[tuple[MeterReading, Optional[dict], Optional[str]]] = []
        for r in targets:
            user = r.user
            room = user.room if user else None
            if not user or not room:
                recalc_results.append((r, None, "no user/room"))
                continue
            tariff = tariff_cache.get_effective_tariff(user=user, room=room)
            if not tariff:
                # Fallback на любой активный
                tariff = (await db.execute(
                    select(Tariff).where(Tariff.is_active.is_(True))
                )).scalars().first()
            if not tariff:
                recalc_results.append((r, None, "no active tariff"))
                continue

            prev = await find_prev(db, r)
            try:
                breakdown = compute_reading_breakdown(
                    user=user, room=room, tariff=tariff,
                    current_hot=r.hot_water or 0,
                    current_cold=r.cold_water or 0,
                    current_elect=r.electricity or 0,
                    prev_reading=prev,
                )
                recalc_results.append((r, breakdown, None))
            except CalculationError as e:
                recalc_results.append((r, None, f"calc_error: {e}"))

        # Печатаем превью
        ok = [x for x in recalc_results if x[1] is not None]
        bad = [x for x in recalc_results if x[1] is None]
        new_total_sum = sum(
            (b["total_cost"] for _, b, _ in ok), Decimal("0")
        )

        print(f"К пересчёту:                {len(ok)}")
        print(f"  - baseline (cost=0):     {sum(1 for _, b, _ in ok if b['is_baseline'])}")
        print(f"  - реальные начисления:   {sum(1 for _, b, _ in ok if not b['is_baseline'])}")
        print(f"Не получилось пересчитать: {len(bad)}")
        print(f"Сумма новых total_cost:    {float(new_total_sum):,.2f} ₽".replace(",", " "))
        print()

        # Топ-15 самых больших новых начислений
        ok_sorted = sorted(ok, key=lambda x: x[1]["total_cost"], reverse=True)[:15]
        print("Топ-15 пересчётов по сумме:")
        print(f"  {'id':>6} {'user':>5} {'period':<14} {'old':>10} {'new':>14} flag")
        for r, b, _ in ok_sorted:
            old = float(r.total_cost or 0)
            new = float(b["total_cost"])
            pname = (r.period.name if r.period else f"#{r.period_id}")
            flag = "BL" if b["is_baseline"] else ""
            print(
                f"  {r.id:>6} {r.user_id:>5} {pname:<14} "
                f"{old:>10.2f} {new:>14,.2f} {flag}".replace(",", " ")
            )

        if bad:
            print()
            print(f"Не удалось пересчитать ({len(bad)} шт), причины:")
            from collections import Counter
            reasons = Counter(reason for _, _, reason in bad)
            for reason, cnt in reasons.most_common():
                print(f"  {reason}: {cnt}")

        if not args.apply:
            print()
            print("=" * 78)
            print("DRY-RUN. Никаких изменений не сделано.")
            print("Для применения добавьте --apply (после backup).")
            print("=" * 78)
            return 0

        # === APPLY ===
        print()
        print("=" * 78)
        print(f"APPLY: пересчитываю {len(ok)} reading'ов ...")
        print("=" * 78)

        for r, b, _ in ok:
            r.total_cost = b["total_cost"]
            r.total_209 = b["total_209"]
            r.total_205 = b["total_205"]
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
        print(f"Готово. {len(ok)} reading'ов пересчитаны и сохранены.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
