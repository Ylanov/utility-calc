"""add telegram_id to users

Revision ID: add_telegram_id_fix
Revises: 1a8bb7c5c09d
Create Date: 2026-02-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add_telegram_id_fix'
down_revision: Union[str, Sequence[str], None] = '1a8bb7c5c09d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Добавляем колонку telegram_id
    op.add_column('users', sa.Column('telegram_id', sa.String(), nullable=True))
    # Создаем индекс для быстрого поиска
    op.create_index(op.f('ix_users_telegram_id'), 'users', ['telegram_id'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_users_telegram_id'), table_name='users')
    op.drop_column('users', 'telegram_id')