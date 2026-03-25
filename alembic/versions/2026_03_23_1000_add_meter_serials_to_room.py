"""add meter serials to room table

Revision ID: 4a5b6c7d8e9f
Revises: 3c4d5e6f7g8h
Create Date: 2026-03-23 10:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4a5b6c7d8e9f'
down_revision = '3c4d5e6f7g8h'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Добавляем три новые колонки для серийных номеров счетчиков в таблицу rooms
    op.add_column('rooms', sa.Column('hw_meter_serial', sa.String(), nullable=True))
    op.add_column('rooms', sa.Column('cw_meter_serial', sa.String(), nullable=True))
    op.add_column('rooms', sa.Column('el_meter_serial', sa.String(), nullable=True))


def downgrade() -> None:
    # Удаляем колонки в обратном порядке при откате миграции
    op.drop_column('rooms', 'el_meter_serial')
    op.drop_column('rooms', 'cw_meter_serial')
    op.drop_column('rooms', 'hw_meter_serial')