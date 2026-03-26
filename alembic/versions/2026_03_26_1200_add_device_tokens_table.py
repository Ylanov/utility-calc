"""add device tokens table for push notifications

Revision ID: 5e6f7g8h9i0j
Revises: 4a5b6c7d8e9f
Create Date: 2026-03-26 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5e6f7g8h9i0j'
down_revision = '4a5b6c7d8e9f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ======================================================
    # СОЗДАЕМ ТАБЛИЦУ DEVICE TOKENS
    # ======================================================
    op.create_table(
        'device_tokens',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token', sa.String(), nullable=False),
        sa.Column('device_type', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),

        sa.ForeignKeyConstraint(
            ['user_id'],
            ['users.id'],
            ondelete='CASCADE'
        )
    )

    # ======================================================
    # ИНДЕКСЫ
    # ======================================================
    op.create_index(
        'ix_device_tokens_token',
        'device_tokens',
        ['token'],
        unique=True
    )

    op.create_index(
        'ix_device_tokens_user_id',
        'device_tokens',
        ['user_id'],
        unique=False
    )


def downgrade() -> None:
    # ======================================================
    # УДАЛЯЕМ ТАБЛИЦУ
    # ======================================================
    op.drop_index('ix_device_tokens_user_id', table_name='device_tokens')
    op.drop_index('ix_device_tokens_token', table_name='device_tokens')
    op.drop_table('device_tokens')