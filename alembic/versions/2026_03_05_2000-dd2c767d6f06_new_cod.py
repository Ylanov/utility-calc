"""new-cod

Revision ID: dd2c767d6f06
Revises: 9f3c34acea67
Create Date: 2026-03-05 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'dd2c767d6f06'
down_revision = '9f3c34acea67'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- создание таблицы system_settings ---
    op.create_table(
        'system_settings',
        sa.Column('key', sa.String(), nullable=False),
        sa.Column('value', sa.String(), nullable=False),
        sa.Column('description', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('key')
    )

    op.create_index(
        op.f('ix_system_settings_key'),
        'system_settings',
        ['key'],
        unique=False
    )

    # --- ПРАВИЛЬНОЕ добавление колонок в partitioned table ---
    op.execute("""
        ALTER TABLE public.readings
        ADD COLUMN IF NOT EXISTS edit_count INTEGER,
        ADD COLUMN IF NOT EXISTS edit_history JSONB,
        ADD COLUMN IF NOT EXISTS anomaly_score INTEGER DEFAULT 0;
    """)


def downgrade() -> None:
    # --- удаление колонок из partitioned table ---
    op.execute("""
        ALTER TABLE public.readings
        DROP COLUMN IF EXISTS anomaly_score,
        DROP COLUMN IF EXISTS edit_history,
        DROP COLUMN IF EXISTS edit_count;
    """)

    # --- удаление таблицы system_settings ---
    op.drop_index(
        op.f('ix_system_settings_key'),
        table_name='system_settings'
    )

    op.drop_table('system_settings')