"""residency_001 — Room.is_vacant + индекс для существующей RoomAssignment.

Bug X. Архитектурно нужно: «жилец переехал → старая комната Vacant
(не удаляется), создаётся новая запись о проживании».

Проверил models.py — таблица room_assignments уже есть (RoomAssignment
с полями moved_in_at / moved_out_at). Будем использовать её, миграция
только добавит:
  1. Колонку rooms.is_vacant (bool, default False, index)

Это позволяет UI показывать «свободная квартира» отдельно, а endpoint
move-to-room автоматически помечать комнату is_vacant=True при выезде
последнего жильца.
"""
from alembic import op
import sqlalchemy as sa


revision = 'residency_001'
down_revision = 'debts_002_obor'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'rooms',
        sa.Column('is_vacant', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.create_index('idx_rooms_vacant', 'rooms', ['is_vacant'])


def downgrade() -> None:
    op.drop_index('idx_rooms_vacant', table_name='rooms')
    op.drop_column('rooms', 'is_vacant')
