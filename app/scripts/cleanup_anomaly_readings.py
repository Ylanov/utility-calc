"""Cleanup аномальных reading'ов с overflow в показаниях счётчиков.

Связан с фиксом валидации в gsheets_sync.py (MAX_PLAUSIBLE_METER_VALUE).
Чистит исторические данные, импортированные ДО фикса:
  - readings, у которых hot_water или cold_water > MAX_PLAUSIBLE_METER_VALUE
    (= 100 000 единиц), типичный мусор от пропущенной десятичной точки в
    показаниях вида «01427.957» → «01427957»;
  - привязанные к ним gsheets_import_rows: сбрасываются reading_id=NULL,
    status='conflict', conflict_reason — чтобы админ увидел в админке
    и попросил жильцов подать показания заново.

ВАЖНО: по умолчанию работает в DRY-RUN — печатает что СОБИРАЕТСЯ
сделать, но НИЧЕГО не меняет. Для реального выполнения нужен флаг --apply.

Использование:

    # 1. Посмотреть что будет тронуто (НИЧЕГО не меняет):
    docker exec utility_calc_web_jkh python -m app.scripts.cleanup_anomaly_readings

    # 2. Сделать БЭКАП БД (через существующий backup-сервис):
    docker exec utility_calc_backup /usr/local/bin/backup.sh

    # 3. Применить cleanup:
    docker exec utility_calc_web_jkh python -m app.scripts.cleanup_anomaly_readings --apply

    # 4. Проверить результат — sum_total_cost должен сильно упасть:
    docker exec utility_calc_web_jkh python -m app.scripts.audit_calculations

Что именно делает (для каждого «битого» reading):
  - total_cost = 0
  - total_209 = 0
  - total_205 = 0
  - is_approved = False  (вернуть в очередь на ручную проверку админом)
  - anomaly_flags = 'DATA_OVERFLOW_RESET'
  - anomaly_score = 100  (помечает как требующее внимания в админке)

Для всех связанных gsheets_import_rows:
  - reading_id = NULL
  - status = 'conflict'
  - conflict_reason = 'value_too_large_data_overflow_<hot>/<cold>'
  - processed_at = NULL  (вернуть в очередь sync)
"""
from __future__ import annotations

import asyncio
from argparse import ArgumentParser
from decimal import Decimal

from sqlalchemy import select, update, or_

from app.core.database import AsyncSessionLocal
from app.modules.utility.models import GSheetsImportRow, MeterReading
from app.modules.utility.services.reading_validators import (
    MAX_WATER_METER_VALUE,
    MAX_ELECTRICITY_METER_VALUE,
    MAX_TOTAL_COST_PER_READING,
)


async def find_anomalies(db) -> list[MeterReading]:
    """Находит approved readings с хотя бы одним из признаков аномалии:
      - hot_water или cold_water превышает MAX_WATER_METER_VALUE
        (overflow от пропущенной десятичной точки в показаниях);
      - electricity превышает MAX_ELECTRICITY_METER_VALUE
        (тест-данные, опечатки);
      - total_cost превышает MAX_TOTAL_COST_PER_READING — даже если
        отдельные показания «в пределах», их комбинация выдаёт счёт
        который физически невозможен для квартиры в общежитии (типичный
        счёт 3-8k ₽, потолок 15k для больших семей).
    """
    result = await db.execute(
        select(MeterReading).where(
            or_(
                MeterReading.hot_water > MAX_WATER_METER_VALUE,
                MeterReading.cold_water > MAX_WATER_METER_VALUE,
                MeterReading.electricity > MAX_ELECTRICITY_METER_VALUE,
                MeterReading.total_cost > MAX_TOTAL_COST_PER_READING,
            )
        )
    )
    return list(result.scalars().all())


async def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Реально выполнить cleanup. Без флага — DRY-RUN (только показать).",
    )
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        anomalies = await find_anomalies(db)

        print(f"Найдено readings с аномалиями: {len(anomalies)}")
        print(f"(критерий: hot/cold > {MAX_WATER_METER_VALUE} ИЛИ "
              f"elect > {MAX_ELECTRICITY_METER_VALUE} ИЛИ "
              f"total_cost > {MAX_TOTAL_COST_PER_READING} ₽)")
        print()

        if not anomalies:
            print("OK — нечего чистить.")
            return 0

        total_cost_sum = sum(
            (r.total_cost or Decimal("0")) for r in anomalies
        )
        print(f"Суммарный «битый» total_cost: "
              f"{float(total_cost_sum):,.2f} ₽".replace(",", " "))
        print()

        print("Список (первые 50):")
        print(f"  {'reading_id':>10} {'user':>5} {'period':>6} "
              f"{'hot_water':>14} {'cold_water':>14} {'total_cost':>16}  flags")
        for r in anomalies[:50]:
            print(
                f"  {r.id:>10} {r.user_id:>5} {r.period_id:>6} "
                f"{float(r.hot_water or 0):>14,.3f} "
                f"{float(r.cold_water or 0):>14,.3f} "
                f"{float(r.total_cost or 0):>16,.2f}  {r.anomaly_flags or ''}"
            )

        # Связанные gsheets-строки — оценим, сколько будет переоткрыто.
        reading_ids = [r.id for r in anomalies]
        sheet_rows_count_result = await db.execute(
            select(GSheetsImportRow).where(
                GSheetsImportRow.reading_id.in_(reading_ids)
            )
        )
        sheet_rows = list(sheet_rows_count_result.scalars().all())
        print()
        print(f"Связанных gsheets_import_rows: {len(sheet_rows)} "
              f"(вернутся в status=conflict для ручного разбора)")

        if not args.apply:
            print()
            print("=" * 78)
            print("DRY-RUN. Никаких изменений не сделано.")
            print("Чтобы применить — добавьте --apply (после бэкапа БД).")
            print("=" * 78)
            return 0

        # === РЕАЛЬНОЕ ПРИМЕНЕНИЕ ===
        print()
        print("=" * 78)
        print(f"APPLY: чищу {len(anomalies)} reading'ов и "
              f"{len(sheet_rows)} gsheets-строк ...")
        print("=" * 78)

        # 1. Reset readings
        for r in anomalies:
            r.total_cost = Decimal("0.00")
            r.total_209 = Decimal("0.00")
            r.total_205 = Decimal("0.00")
            r.is_approved = False
            r.anomaly_flags = "DATA_OVERFLOW_RESET"
            r.anomaly_score = 100
            db.add(r)

        # 2. Reset gsheets_import_rows. Используем UPDATE...IN(...) чтобы
        # не дёргать каждую строку Python-объектом.
        await db.execute(
            update(GSheetsImportRow)
            .where(GSheetsImportRow.reading_id.in_(reading_ids))
            .values(
                reading_id=None,
                status="conflict",
                processed_at=None,
                conflict_reason=(
                    "data_overflow_cleanup: показания или итог превысили "
                    "санитарные пороги (вода >10000, электр. >50000, "
                    "total_cost >100000 ₽). Проверьте формат — вероятно "
                    "пропущена десятичная точка в показании счётчика."
                ),
            )
        )

        await db.commit()

        print()
        print(f"Готово. Сброшено {len(anomalies)} readings, "
              f"{len(sheet_rows)} gsheets-строк вернулись в conflict.")
        print("Запустите audit_calculations для проверки.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
