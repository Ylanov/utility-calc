"""gsheets_import_rows.reading_id: ondelete=SET NULL — NOOP

Revision ID: perf_002_gsheets_reading_set_null
Revises: perf_001_readings_compound
Create Date: 2026-04-30 12:00:00.000000

ПУСТАЯ МИГРАЦИЯ (apr 2026).
Изначально я предполагал, что у gsheets_import_rows.reading_id есть FK
к readings.id, и хотел добавить ondelete='SET NULL', чтобы DELETE reading
не падал с FK violation. Но в исходной миграции gsheets_001_import_rows
явно стоит:

    # readings — партиционированная таблица, FK на неё в PostgreSQL
    # нельзя, поэтому reading_id не имеет constraint — проверяем
    # вручную в коде.

То есть FK в БД физически не создавался — drop_constraint падал на
"constraint does not exist". Миграция оставлена как noop, чтобы цепочка
ревизий не сломалась у тех, кто уже применил её локально.

Защита от 500-ошибки на DELETE /api/admin/readings/{id} реализована
кодом в admin_readings_manual.delete_reading: перед удалением reading
явно отвязываются связанные gsheets_import_rows
(reading_id=NULL, processed_at=NULL).
"""
from alembic import op  # noqa: F401


revision = 'perf_002_gsheets_reading_set_null'
down_revision = 'perf_001_readings_compound'
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
