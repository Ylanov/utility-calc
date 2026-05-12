"""debts_002 — DebtImportLog.archive_path + retention column.

Раньше файл лежал в /app/static/temp_imports/{uuid}.xlsx без привязки
к DebtImportLog. После рестарта контейнера временные файлы могли
исчезнуть — оригинал ОСВ из 1С терялся, и админ не мог скачать его
обратно.

Теперь:
  - archive_path хранит путь к сохранённому xlsx (постоянный
    /app/data/debt_archives/{log_id}.xlsx, retention 730 дней)
  - retention_days — индивидуальная настройка retention для лога
    (по умолчанию NULL → берём из analyzer_settings.debt.archive_retention_days)

Поля nullable — старые DebtImportLog продолжают работать без archive
(downgrade ничего не теряет).
"""
from alembic import op
import sqlalchemy as sa


revision = 'debts_002_archive_path'
down_revision = 'integrity_003_reading_bounds'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'debt_import_logs',
        sa.Column('archive_path', sa.String(length=512), nullable=True),
    )
    op.add_column(
        'debt_import_logs',
        sa.Column('retention_days', sa.Integer(), nullable=True),
    )
    # batch_id — для группировки парных импортов (205 + 209 одной загрузкой).
    # Тот же uuid в обоих DebtImportLog → в UI показываем «1 импорт из 2 файлов».
    op.add_column(
        'debt_import_logs',
        sa.Column('batch_id', sa.String(length=36), nullable=True, index=True),
    )
    op.create_index(
        'idx_debt_import_logs_batch_id', 'debt_import_logs', ['batch_id'],
    )


def downgrade() -> None:
    op.drop_index('idx_debt_import_logs_batch_id', table_name='debt_import_logs')
    op.drop_column('debt_import_logs', 'batch_id')
    op.drop_column('debt_import_logs', 'retention_days')
    op.drop_column('debt_import_logs', 'archive_path')
