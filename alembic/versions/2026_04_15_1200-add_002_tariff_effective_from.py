"""Add effective_from to tariffs

Revision ID: add_002_tariff_effective_from
Revises: fix_002_audit_log_updated_at
Create Date: 2026-04-15 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'add_002_tariff_effective_from'
down_revision: Union[str, Sequence[str], None] = 'fix_002_audit_log_updated_at'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Добавляем поле effective_from в таблицу тарифов.
    # nullable=True — не ломаем существующие записи.
    # Если поле NULL — тариф работает по-прежнему (is_active управляется вручную).
    # Если поле задано — тариф автоматически активируется Celery-задачей в эту дату.
    op.add_column(
        'tariffs',
        sa.Column('effective_from', sa.DateTime(), nullable=True)
    )
    op.create_index('idx_tariff_effective_from', 'tariffs', ['effective_from'])


def downgrade() -> None:
    op.drop_index('idx_tariff_effective_from', table_name='tariffs')
    op.drop_column('tariffs', 'effective_from')
