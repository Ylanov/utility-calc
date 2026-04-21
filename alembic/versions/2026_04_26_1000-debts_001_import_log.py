"""Debt import log for 1C Excel uploads

Revision ID: debts_001_import_log
Revises: recalc_001_period_recalc_jobs
Create Date: 2026-04-26 10:00:00.000000

История импортов долгов из 1С. Нужна, чтобы:
  * админ видел «кто, когда и какой файл» заливал;
  * при ошибке в 1С-выгрузке можно было откатить конкретный импорт
    (восстановить предыдущие debt_209/205/overpayment_* по snapshot);
  * «не найденные» ФИО собирались в структурированный список, к которому
    можно вернуться и привязать к конкретному жильцу вручную.

Структура:
  * started_at / completed_at: хронология и длительность;
  * file_name: оригинальное имя Excel (без пути);
  * account_type: "209" | "205";
  * processed / updated / created / not_found_count: агрегированные счётчики;
  * not_found_users: JSONB-массив ФИО, не найденных fuzzy-матчером;
  * snapshot_data: JSONB-мапа {reading_id: {debt_209, overpayment_209, debt_205, overpayment_205}}
                   — для отката. Ограничиваем 30 днями хранения на уровне автоочистки.
  * status: pending/completed/failed/reverted — жизненный цикл.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'debts_001_import_log'
down_revision = 'recalc_001_period_recalc_jobs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'debt_import_logs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('account_type', sa.String(8), nullable=False),
        sa.Column('period_id', sa.Integer(), nullable=True),
        sa.Column('file_name', sa.String(255), nullable=True),
        sa.Column('status', sa.String(24), nullable=False, server_default='pending'),
        sa.Column('started_by_id', sa.Integer(), nullable=True),
        sa.Column('started_by_username', sa.String(128), nullable=True),
        sa.Column('processed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('not_found_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('not_found_users', JSONB(), nullable=True),
        sa.Column('snapshot_data', JSONB(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('reverted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['period_id'], ['periods.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['started_by_id'], ['users.id'], ondelete='SET NULL'),
    )
    op.create_index('idx_debt_import_logs_started_at', 'debt_import_logs', ['started_at'])
    op.create_index('idx_debt_import_logs_status', 'debt_import_logs', ['status'])


def downgrade() -> None:
    op.drop_index('idx_debt_import_logs_status', table_name='debt_import_logs')
    op.drop_index('idx_debt_import_logs_started_at', table_name='debt_import_logs')
    op.drop_table('debt_import_logs')
