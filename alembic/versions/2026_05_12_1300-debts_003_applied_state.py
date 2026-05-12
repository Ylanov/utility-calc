"""debts_003 — DebtImportLog.applied_state для быстрого diff между импортами.

snapshot_data уже хранит state ДО импорта (для undo). Чтобы сравнить два
импорта одного account_type, нужен и state ПОСЛЕ — иначе пришлось бы:
  - либо парсить оригинальный xlsx заново (медленно, ~секунды на 600 строк)
  - либо стыковать snapshot_data соседних логов в хронологическом порядке
    (хрупко, ломается если порядок импортов 209→205→209→205)

applied_state — JSON {room_id: {debt_209, overpayment_209, debt_205,
overpayment_205, username, room_label}}. denormalized чтобы diff
не делал JOIN на user/room для каждой строки.

Размер: ~50 байт на жильца × 500 жильцов = 25 KB на импорт. На 24
импорта в год = 600 KB — пренебрежимо.

Поле nullable — старые логи (до миграции) останутся без applied_state,
diff для них вернёт «не поддерживается, перезагрузите файлы».
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'debts_003_applied_state'
down_revision = 'debts_002_archive_path'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'debt_import_logs',
        sa.Column('applied_state', JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column('debt_import_logs', 'applied_state')
