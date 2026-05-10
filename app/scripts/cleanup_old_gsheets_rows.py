"""Физическое удаление исторических gsheets_import_rows.

Жильцы оставляют в гугл-таблице многолетнюю историю показаний (видно
на проде: у одного жильца 34 подачи за 2023-2026 годы). Они не нужны —
актуальны только последние 2-3 месяца. Этот скрипт удаляет всё что
старше указанного порога.

Связан с фильтром в gsheets_sync.py:_max_age_days() (по умолчанию 90):
скрипт чистит уже накопленные строки, фильтр не даёт новым попасть.

ВАЖНО: dry-run по умолчанию. Для реального применения — флаг --apply.
Удаление физическое (DELETE), не reject — после --apply строки исчезают.
ON DELETE SET NULL на reading_id (миграция perf_002) — readings не
удаляются, у них просто разрывается связь с gsheets-источником.

Использование:

    # Посмотреть сколько строк попадёт под удаление (НИЧЕГО не меняет):
    docker exec utility_calc_web_jkh python -m app.scripts.cleanup_old_gsheets_rows

    # Применить (после backup):
    docker exec utility_calc_backup /usr/local/bin/backup.sh
    docker exec utility_calc_web_jkh python -m app.scripts.cleanup_old_gsheets_rows --apply

    # Изменить порог: «старше 30 дней»:
    docker exec utility_calc_web_jkh python -m app.scripts.cleanup_old_gsheets_rows --older-than-days 30 --apply

ПРИМЕЧАНИЕ: критерий — sheet_timestamp (дата подачи в гугл-таблице),
НЕ created_at (когда строка попала в нашу БД). Это правильно, так как
admin может загрузить старую таблицу разово, и тогда created_at у всех
строк свежий, а sheet_timestamp реально старый.
"""
from __future__ import annotations

import asyncio
from argparse import ArgumentParser
from datetime import timedelta

from sqlalchemy import delete, func, select

from app.core.database import AsyncSessionLocal
from app.core.time_utils import utcnow
from app.modules.utility.models import GSheetsImportRow


DEFAULT_AGE_DAYS = 90


async def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Применить (по умолчанию — DRY-RUN, ничего не меняет).",
    )
    parser.add_argument(
        "--older-than-days",
        type=int,
        default=DEFAULT_AGE_DAYS,
        help=f"Порог возраста строки в днях. По умолчанию {DEFAULT_AGE_DAYS}. "
             f"0 захватит все строки с непустым sheet_timestamp.",
    )
    args = parser.parse_args()

    cutoff = utcnow() - timedelta(days=args.older_than_days)

    async with AsyncSessionLocal() as db:
        total_q = await db.execute(
            select(func.count(GSheetsImportRow.id))
        )
        total_rows = total_q.scalar_one()

        target_q = await db.execute(
            select(func.count(GSheetsImportRow.id)).where(
                GSheetsImportRow.sheet_timestamp.is_not(None),
                GSheetsImportRow.sheet_timestamp < cutoff,
            )
        )
        target_count = target_q.scalar_one()

        # Распределение по статусам — чтобы было видно что именно стирается
        status_q = await db.execute(
            select(GSheetsImportRow.status, func.count(GSheetsImportRow.id))
            .where(
                GSheetsImportRow.sheet_timestamp.is_not(None),
                GSheetsImportRow.sheet_timestamp < cutoff,
            )
            .group_by(GSheetsImportRow.status)
        )
        by_status = dict(status_q.all())

        print(f"Всего gsheets_import_rows в БД: {total_rows}")
        print(f"Под удаление (sheet_timestamp < {cutoff:%Y-%m-%d %H:%M}): {target_count}")
        print()
        if by_status:
            print("Распределение по статусам:")
            for st, cnt in sorted(by_status.items(), key=lambda x: -x[1]):
                print(f"  {st:<20} {cnt:>6}")
        print()

        # Несколько примеров
        sample_q = await db.execute(
            select(
                GSheetsImportRow.id,
                GSheetsImportRow.sheet_timestamp,
                GSheetsImportRow.matched_user_id,
                GSheetsImportRow.raw_fio,
                GSheetsImportRow.status,
            ).where(
                GSheetsImportRow.sheet_timestamp.is_not(None),
                GSheetsImportRow.sheet_timestamp < cutoff,
            )
            .order_by(GSheetsImportRow.sheet_timestamp)
            .limit(15)
        )
        print("Примеры (старейшие 15):")
        print(f"  {'id':>8} {'sheet_ts':<20} {'user':>5} {'status':<16} fio")
        for r in sample_q.all():
            print(
                f"  {r[0]:>8} {r[1].strftime('%Y-%m-%d %H:%M'):<20} "
                f"{r[2] or 0:>5} {r[3]:<16} {r[4][:40]}"
            )

        if target_count == 0:
            print()
            print("OK — нечего удалять.")
            return 0

        if not args.apply:
            print()
            print("=" * 78)
            print("DRY-RUN. Никаких изменений не сделано.")
            print("Чтобы УДАЛИТЬ — добавьте --apply (после backup).")
            print("=" * 78)
            return 0

        # === APPLY: физическое удаление ===
        print()
        print("=" * 78)
        print(f"APPLY: удаляю {target_count} строк ...")
        print("=" * 78)

        await db.execute(
            delete(GSheetsImportRow).where(
                GSheetsImportRow.sheet_timestamp.is_not(None),
                GSheetsImportRow.sheet_timestamp < cutoff,
            )
        )
        await db.commit()

        print(f"Готово. {target_count} строк физически удалены из gsheets_import_rows.")
        print("(Привязанные readings не пострадали — FK с ON DELETE SET NULL.)")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
