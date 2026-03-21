"""new-cod

Revision ID: dd2c767d6f06
Revises: 9f3c34acea67
Create Date: 2026-03-05 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


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

    # --- добавление новых колонок в readings ---
    op.add_column(
        'readings',
        sa.Column('edit_count', sa.Integer(), nullable=True)
    )

    op.add_column(
        'readings',
        sa.Column('edit_history', postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    # --- удаление колонок из readings ---
    op.drop_column('readings', 'edit_history')
    op.drop_column('readings', 'edit_count')

    # --- удаление таблицы system_settings ---
    op.drop_index(
        op.f('ix_system_settings_key'),
        table_name='system_settings'
    )

    op.drop_table('system_settings')