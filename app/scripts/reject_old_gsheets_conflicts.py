"""Массовое отклонение «зависших» gsheets_import_rows со status=conflict.

После двух витков cleanup_anomaly_readings.py в БД накопилось ~540
gsheets_import_rows со статусом 'conflict' — это записи которые попали
под санитарные пороги (overflow) или были отвязаны от reading'а при
cleanup'е аномалий. Их нужно либо разобрать в админке руками
(каждую — пометить, привязать к жильцу заново), либо массово отклонить
и попросить жильцов переподать показания.

Этот скрипт делает массовый reject — переводит conflict-строки в
status='rejected' с понятным notes-комментарием. По умолчанию ловит
ТОЛЬКО строки старше 7 дней — свежие конфликты остаются в очереди для
ручного разбора (они могут быть актуальными).

ВАЖНО: dry-run по умолчанию. Для реального применения — флаг --apply.

Использование:

    # Посмотреть какие строки попадут под reject (НИЧЕГО не меняет):
    docker exec utility_calc_web_jkh python -m app.scripts.reject_old_gsheets_conflicts

    # Применить (после backup):
    docker exec utility_calc_backup /usr/local/bin/backup.sh
    docker exec utility_calc_web_jkh python -m app.scripts.reject_old_gsheets_conflicts --apply

    # Изменить порог: «старше 30 дней»:
    docker exec utility_calc_web_jkh python -m app.scripts.reject_old_gsheets_conflicts --older-than-days 30 --apply

    # Включить ВСЕ conflict-строки независимо от даты:
    docker exec utility_calc_web_jkh python -m app.scripts.reject_old_gsheets_conflicts --older-than-days 0 --apply
"""
from __future__ import annotations

import asyncio
from argparse import ArgumentParser
from datetime import timedelta

from sqlalchemy import func, select, update

from app.core.database import AsyncSessionLocal
from app.core.time_utils import utcnow
from app.modules.utility.models import GSheetsImportRow


REJECT_NOTE = (
    "Auto-rejected by reject_old_gsheets_conflicts.py: строка висела в "
    "status='conflict' дольше указанного порога. Жилец должен переподать "
    "показания через мобильное приложение или через гугл-таблицу заново."
)


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
        default=7,
        help="Порог возраста строки. По умолчанию 7 дней. "
             "0 = захватить ВСЕ conflict-строки.",
    )
    args = parser.parse_args()

    cutoff = utcnow() - timedelta(days=args.older_than_days)

    async with AsyncSessionLocal() as db:
        # Общая статистика по conflict-строкам.
        total_q = await db.execute(
            select(func.count(GSheetsImportRow.id)).where(
                GSheetsImportRow.status == "conflict"
            )
        )
        total_conflict = total_q.scalar_one()

        # Что попадёт под reject.
        target_q = await db.execute(
            select(GSheetsImportRow).where(
                GSheetsImportRow.status == "conflict",
                GSheetsImportRow.created_at < cutoff,
            )
        )
        targets = list(target_q.scalars().all())

        print(f"Всего gsheets-строк в status='conflict': {total_conflict}")
        print(f"Старше {args.older_than_days} дней (созданы до {cutoff:%Y-%m-%d %H:%M}): "
              f"{len(targets)}")
        print()

        if not targets:
            print("OK — нечего отклонять.")
            return 0

        # Краткий вывод (первые 30).
        print("Список первых 30 (для быстрого глаза):")
        print(f"  {'id':>7} {'user':>5} {'created':>20}  fio")
        for r in targets[:30]:
            print(
                f"  {r.id:>7} {r.matched_user_id or 0:>5} "
                f"{r.created_at:%Y-%m-%d %H:%M}  {r.raw_fio[:40]}"
            )
        if len(targets) > 30:
            print(f"  ... и ещё {len(targets) - 30}")

        if not args.apply:
            print()
            print("=" * 78)
            print("DRY-RUN. Никаких изменений не сделано.")
            print("Чтобы отклонить — добавьте --apply (после backup).")
            print("=" * 78)
            return 0

        # === APPLY ===
        print()
        print("=" * 78)
        print(f"APPLY: отклоняю {len(targets)} строк ...")
        print("=" * 78)

        ids = [r.id for r in targets]
        await db.execute(
            update(GSheetsImportRow)
            .where(GSheetsImportRow.id.in_(ids))
            .values(
                status="rejected",
                processed_at=utcnow(),
                notes=REJECT_NOTE,
            )
        )
        await db.commit()

        print(f"Готово. {len(targets)} строк помечены status='rejected'.")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
