"""gsheets_import_rows.reading_id: ondelete=SET NULL

Revision ID: perf_002_gsheets_reading_set_null
Revises: perf_001_readings_compound
Create Date: 2026-04-30 12:00:00.000000

Раньше FK gsheets_import_rows.reading_id -> readings.id создавался без
ondelete-правила. Когда админ удалял MeterReading (например, чтобы
re-utvердить gsheets-импорт), PostgreSQL рейзил 23503 (foreign_key_violation),
а API падал с 500 Internal Server Error.

Теперь ondelete='SET NULL' — при удалении reading связанные gsheets-строки
автоматически отвязываются, статус остаётся 'auto_approved', и следующий
прогон promote_auto_approved_rows() создаёт новый MeterReading.

Старый именованный constraint Alembic auto-generation называл по схеме
gsheets_import_rows_reading_id_fkey (PG default).
"""
from alembic import op


revision = 'perf_002_gsheets_reading_set_null'
down_revision = 'perf_001_readings_compound'
branch_labels = None
depends_on = None

_FK_NAME = 'gsheets_import_rows_reading_id_fkey'
_TABLE = 'gsheets_import_rows'
_COL = 'reading_id'
_REF_TABLE = 'readings'
_REF_COL = 'id'


def upgrade() -> None:
    # PG не умеет ALTER CONSTRAINT с изменением ondelete — drop + recreate.
    op.drop_constraint(_FK_NAME, _TABLE, type_='foreignkey')
    op.create_foreign_key(
        _FK_NAME,
        _TABLE, _REF_TABLE,
        [_COL], [_REF_COL],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint(_FK_NAME, _TABLE, type_='foreignkey')
    op.create_foreign_key(
        _FK_NAME,
        _TABLE, _REF_TABLE,
        [_COL], [_REF_COL],
    )
