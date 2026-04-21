"""Period recalculation jobs

Revision ID: recalc_001_period_recalc_jobs
Revises: domain_001_resident_types
Create Date: 2026-04-25 10:00:00.000000

Фоновая задача «Полный перерасчёт периода» — нужна, когда:
  * админ обновил тарифы, а показания за текущий период уже утверждены
    со старыми (или нулевыми) тарифами;
  * Room.tariff_id поменялся для части комнат;
  * жильцам меняли резидент-тип (family↔single) после утверждения.

Нам нужна аудит-таблица, чтобы админ:
  1) запускал preview (read-only прогон calculate_utilities) — видел, кому
     и насколько изменится сумма, без апдейтов;
  2) применял пересчёт (апдейт total_209/total_205/cost_* полей) — с
     прогрессом и возможностью посмотреть историю.

Таблица хранит:
  * целевой period_id,
  * текущий статус: preview_pending → preview_ready → apply_pending → done/failed/cancelled;
  * агрегированный diff_summary (JSONB) — totals + топ примеров для модалки;
  * связь со своим Celery-task-id (для отмены / повторной привязки при рестарте).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'recalc_001_period_recalc_jobs'
down_revision = 'domain_001_resident_types'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'recalc_jobs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('period_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False,
                  server_default='preview_pending'),
        sa.Column('progress', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('total_readings', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('processed', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('diff_summary', JSONB(), nullable=True),
        sa.Column('started_by_id', sa.Integer(), nullable=True),
        sa.Column('started_by_username', sa.String(128), nullable=True),
        sa.Column('celery_task_id', sa.String(64), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('applied_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['period_id'], ['periods.id'],
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['started_by_id'], ['users.id'],
                                ondelete='SET NULL'),
    )
    op.create_index('idx_recalc_jobs_period_created',
                    'recalc_jobs', ['period_id', 'created_at'])
    op.create_index('idx_recalc_jobs_status', 'recalc_jobs', ['status'])


def downgrade() -> None:
    op.drop_index('idx_recalc_jobs_status', table_name='recalc_jobs')
    op.drop_index('idx_recalc_jobs_period_created', table_name='recalc_jobs')
    op.drop_table('recalc_jobs')
