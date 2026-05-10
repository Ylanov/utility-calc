"""Аудит расчётов utility-calc — выявление аномальных сумм в БД.

Запуск на сервере:

    docker compose exec web python -m app.scripts.audit_calculations

Скрипт ничего не меняет в БД — только читает и печатает отчёт. Цель:
быстро локализовать источник аномалий типа «Начислено 1.48 млрд за один
месяц у 319 жильцов».

Что проверяется:
  1) Topline по 6 последним периодам: количество approved readings
     и SUM(total_cost). Здесь сразу видно «который месяц сломан».
  2) Top-30 readings по total_cost — кандидаты на аномалии. Иногда
     одна-две записи с total_cost = 100 млн объясняют всю сумму.
  3) Distribution (min / avg / max) по 3 последним периодам. Если max
     далеко от avg — есть отдельные выбросы.
  4) Invariant total_cost == total_209 + total_205. Где не совпадает —
     значит код сохраняет рассогласованные суммы (потенциальный баг).
  5) Записи с total_cost > 100 000 ₽ — заведомо аномальные для квартиры
     в общежитии.
  6) Дубликаты: одной (user_id, period_id) пары соответствует более
     одной approved-записи. Это — типичный источник «удвоенных» сумм
     при SUM на дашборде.
  7) Adjustments по периодам — суммы и количество. Гигантская
     корректировка тоже даёт скачок KPI.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.modules.utility.models import (
    Adjustment,
    BillingPeriod,
    MeterReading,
)


def fmt(v) -> str:
    """Деньги в формате 1 234 567,89 ₽."""
    if v is None:
        return "—"
    if not isinstance(v, Decimal):
        v = Decimal(str(v))
    s = f"{v:,.2f}"
    # 1,234,567.89 → 1 234 567,89
    return s.replace(",", " ").replace(".", ",") + " ₽"


def hr(label: str = "") -> None:
    print()
    print("=" * 78)
    if label:
        print(label)
        print("=" * 78)


async def main() -> int:
    async with AsyncSessionLocal() as db:
        periods = (
            await db.execute(
                select(BillingPeriod).order_by(BillingPeriod.id.desc()).limit(6)
            )
        ).scalars().all()

        if not periods:
            print("В БД нет ни одного BillingPeriod — нечего аудировать.")
            return 0

        # 1) PER-PERIOD TOTALS
        hr("1) ПЕРИОДЫ — количество approved readings и SUM(total_cost)")
        print(f"  {'period':<25} {'count':>8}  {'sum_total_cost':>22}")
        for p in periods:
            r = (
                await db.execute(
                    select(
                        func.count(MeterReading.id),
                        func.coalesce(func.sum(MeterReading.total_cost), 0),
                    ).where(
                        MeterReading.period_id == p.id,
                        MeterReading.is_approved.is_(True),
                    )
                )
            ).one()
            print(f"  {p.name:<25} {r[0]:>8}  {fmt(r[1]):>22}")

        # 2) TOP-30 BY total_cost
        hr("2) TOP-30 readings по total_cost (все периоды)")
        rows = (
            await db.execute(
                select(
                    MeterReading.id,
                    MeterReading.user_id,
                    MeterReading.period_id,
                    MeterReading.total_cost,
                    MeterReading.total_209,
                    MeterReading.total_205,
                    MeterReading.is_approved,
                    MeterReading.anomaly_flags,
                )
                .order_by(MeterReading.total_cost.desc())
                .limit(30)
            )
        ).all()
        period_name_by_id = {p.id: p.name for p in periods}
        print(
            f"  {'id':>7} {'user':>6} {'period':<14} {'total_cost':>16} "
            f"{'total_209':>16} {'total_205':>16} appr  flags"
        )
        for r in rows:
            pname = period_name_by_id.get(r[2], f"#{r[2]}")
            print(
                f"  {r[0]:>7} {r[1]:>6} {pname:<14} {fmt(r[3]):>16} "
                f"{fmt(r[4]):>16} {fmt(r[5]):>16}  {'Y' if r[6] else 'N'}   {r[7] or ''}"
            )

        # 3) DISTRIBUTION
        hr("3) DISTRIBUTION (min / avg / max) по 3 последним периодам")
        for p in periods[:3]:
            stats = (
                await db.execute(
                    select(
                        func.min(MeterReading.total_cost),
                        func.avg(MeterReading.total_cost),
                        func.max(MeterReading.total_cost),
                        func.count(MeterReading.id),
                    ).where(
                        MeterReading.period_id == p.id,
                        MeterReading.is_approved.is_(True),
                    )
                )
            ).one()
            print(
                f"  {p.name}: count={stats[3]}  "
                f"min={fmt(stats[0])}  avg={fmt(stats[1])}  max={fmt(stats[2])}"
            )

        # 4) INVARIANT total_cost == total_209 + total_205
        hr("4) ИНВАРИАНТ total_cost ≈ total_209 + total_205 (approved)")
        broken = (
            await db.execute(
                select(
                    MeterReading.id,
                    MeterReading.user_id,
                    MeterReading.period_id,
                    MeterReading.total_cost,
                    MeterReading.total_209,
                    MeterReading.total_205,
                    MeterReading.anomaly_flags,
                )
                .where(
                    MeterReading.is_approved.is_(True),
                    func.abs(
                        MeterReading.total_cost
                        - func.coalesce(MeterReading.total_209, 0)
                        - func.coalesce(MeterReading.total_205, 0)
                    )
                    > Decimal("0.01"),
                )
                .limit(20)
            )
        ).all()
        if not broken:
            print("  OK — инвариант выполняется")
        else:
            print(f"  НАРУШЕН для {len(broken)}+ записей (показаны первые 20):")
            for r in broken:
                pname = period_name_by_id.get(r[2], f"#{r[2]}")
                diff = (r[3] or 0) - (r[4] or 0) - (r[5] or 0)
                print(
                    f"    id={r[0]:>6} user={r[1]:>5} period={pname:<14} "
                    f"total={fmt(r[3]):>14} 209={fmt(r[4]):>14} "
                    f"205={fmt(r[5]):>14} diff={fmt(diff):>14}  {r[6] or ''}"
                )

        # 5) ANOMALIES > 100k
        hr("5) АНОМАЛИИ: approved-readings с total_cost > 100 000 ₽")
        crazy = (
            await db.execute(
                select(
                    MeterReading.id,
                    MeterReading.user_id,
                    MeterReading.period_id,
                    MeterReading.total_cost,
                    MeterReading.anomaly_flags,
                )
                .where(
                    MeterReading.is_approved.is_(True),
                    MeterReading.total_cost > Decimal("100000"),
                )
                .order_by(MeterReading.total_cost.desc())
                .limit(50)
            )
        ).all()
        print(f"  Найдено: {len(crazy)} (показано до 50)")
        for r in crazy:
            pname = period_name_by_id.get(r[2], f"#{r[2]}")
            print(
                f"    id={r[0]:>6} user={r[1]:>5} period={pname:<14} "
                f"total={fmt(r[3]):>16}   {r[4] or ''}"
            )

        # 6) DUPLICATES per (user_id, period_id)
        hr("6) ДУБЛИКАТЫ: >1 approved reading на (user_id, period_id)")
        dups = (
            await db.execute(
                select(
                    MeterReading.user_id,
                    MeterReading.period_id,
                    func.count(MeterReading.id),
                    func.sum(MeterReading.total_cost),
                )
                .where(MeterReading.is_approved.is_(True))
                .group_by(MeterReading.user_id, MeterReading.period_id)
                .having(func.count(MeterReading.id) > 1)
                .order_by(func.count(MeterReading.id).desc())
                .limit(30)
            )
        ).all()
        if not dups:
            print("  OK — дубликатов нет")
        else:
            print(f"  Найдено пар: {len(dups)} (показано до 30):")
            print(f"    {'user':>6} {'period':<14} {'cnt':>4}  {'sum_total_cost':>20}")
            for r in dups:
                pname = period_name_by_id.get(r[1], f"#{r[1]}")
                print(f"    {r[0]:>6} {pname:<14} {r[2]:>4}  {fmt(r[3]):>20}")

        # 7) ADJUSTMENTS by period
        hr("7) ADJUSTMENTS — суммы корректировок по периодам")
        for p in periods:
            r = (
                await db.execute(
                    select(
                        func.count(Adjustment.id),
                        func.coalesce(func.sum(Adjustment.amount), 0),
                        func.min(Adjustment.amount),
                        func.max(Adjustment.amount),
                    ).where(Adjustment.period_id == p.id)
                )
            ).one()
            print(
                f"  {p.name:<25} count={r[0]:<5}  sum={fmt(r[1])}  "
                f"min={fmt(r[2])}  max={fmt(r[3])}"
            )

        hr("ЗАКОНЧЕНО")
        print(
            "Скиньте этот вывод обратно в чат — по нему можно будет точно сказать,\n"
            "что именно даёт аномальную сумму на дашборде."
        )
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
