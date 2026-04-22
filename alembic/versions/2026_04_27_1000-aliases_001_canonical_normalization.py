"""Re-normalize gsheets_aliases.alias_fio_normalized to canonical form

Revision ID: aliases_001_canonical_norm
Revises: debts_001_import_log
Create Date: 2026-04-27 10:00:00.000000

Раньше в admin_gsheets.py была своя функция _normalize_fio, которая
НЕ убирала точки: «Иванов И.И.» → «иванов и.и.». Sync-сервис же
нормализовал как «иванов и и» (точки убирает).

В результате alias'ы, сохранённые через «Кто это?», при следующем
импорте не находились — ключи разные. Админ тыкал «Кто это?» для
одного и того же человека каждый месяц.

Фикс унифицировал обе функции: теперь everywhere используется
gsheets_sync.normalize_fio. Но в БД уже накопились старые alias
со старой нормализацией — эта миграция перезаписывает их в новый
формат (без точек, ё→е, коллапс пробелов).
"""
from alembic import op
import sqlalchemy as sa
import re


revision = 'aliases_001_canonical_norm'
down_revision = 'debts_001_import_log'
branch_labels = None
depends_on = None


def _new_normalize(fio: str) -> str:
    """Зеркало gsheets_sync.normalize_fio на момент миграции.
    Если функция в коде эволюционирует — миграция остаётся
    консистентной с тем значением, на которое перешли в этот момент."""
    if not fio:
        return ""
    s = str(fio).lower().replace("ё", "е")
    s = re.sub(r"[.,]", " ", s)
    s = " ".join(s.split())
    return s


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        # raw SQL вместо ORM — модель GSheetsAlias может отличаться
        # между ветками разработки; миграция должна быть независимой.
        sa.text("SELECT id, alias_fio, alias_fio_normalized FROM gsheets_aliases")
    ).fetchall()

    # Собираем уникальные (new_norm, user_id) — если после нормализации
    # две записи схлопнутся в один ключ, оставляем первую, остальные
    # помечаем к удалению (дубли с разным user_id обрабатываем руками —
    # логируем и оставляем КАК ЕСТЬ, чтобы не потерять связки).
    seen_keys = {}
    updates = []
    skipped_dupes = []

    for rid, raw, old_norm in rows:
        # Перенормализуем по свежей функции
        new_norm = _new_normalize(raw or old_norm or "")
        if not new_norm:
            continue
        if new_norm == old_norm:
            continue  # уже в правильном формате

        if new_norm in seen_keys:
            skipped_dupes.append((rid, new_norm, seen_keys[new_norm]))
            continue

        seen_keys[new_norm] = rid
        updates.append((rid, new_norm))

    # Пакетный UPDATE — по одному row, чтобы не триггерить unique-violation.
    upd_stmt = sa.text(
        "UPDATE gsheets_aliases SET alias_fio_normalized = :n WHERE id = :i"
    )
    for rid, new_norm in updates:
        conn.execute(upd_stmt, {"n": new_norm, "i": rid})

    if skipped_dupes:
        # Эти записи оставлены как есть — они продолжат работать через
        # runtime-лукап в старом формате (в build_aliases_index мы читаем
        # и старую нормализацию тоже, так что дубли безобидны).
        print(
            f"[aliases_001] {len(skipped_dupes)} aliases collide after re-normalization "
            "— оставлены со старым ключом, runtime-лукап найдёт через fallback."
        )


def downgrade() -> None:
    # Обратной миграции нет — старый формат безвозвратно не восстановить
    # (мы теряем информацию о точках). Фикс идёт только вперёд.
    pass
