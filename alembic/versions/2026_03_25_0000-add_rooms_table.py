"""add rooms table

Revision ID: add_rooms_table
Revises: 2b3c4d5e6f7g
Create Date: 2026-03-25 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'add_rooms_table'
down_revision = '2b3c4d5e6f7g'  # Указываем предыдущую миграцию
branch_labels = None
depends_on = None


def upgrade():
    # 1. Создаем таблицу rooms
    op.create_table(
        'rooms',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('dormitory_name', sa.String(), index=True),
        sa.Column('room_number', sa.String(), index=True),
        sa.Column('apartment_area', sa.Numeric(10, 2), default=0.00),
        sa.Column('total_room_residents', sa.Integer(), default=1),
        sa.Column('last_hot_water', sa.Numeric(12, 3), default=0.000),
        sa.Column('last_cold_water', sa.Numeric(12, 3), default=0.000),
        sa.Column('last_electricity', sa.Numeric(12, 3), default=0.000),
    )
    op.create_index('uq_room_dormitory_number', 'rooms', ['dormitory_name', 'room_number'], unique=True)

    # 2. Добавляем room_id в users
    op.add_column('users', sa.Column('room_id', sa.Integer(), sa.ForeignKey('rooms.id'), nullable=True))

    # 3. Добавляем room_id в readings
    op.add_column('readings', sa.Column('room_id', sa.Integer(), sa.ForeignKey('rooms.id'), nullable=True))
    op.create_index('idx_reading_room_period', 'readings', ['room_id', 'period_id'])
    op.create_index('idx_reading_room_approved', 'readings', ['room_id', 'is_approved'])


def downgrade():
    op.drop_index('idx_reading_room_approved', table_name='readings')
    op.drop_index('idx_reading_room_period', table_name='readings')
    op.drop_constraint(None, 'readings', type_='foreignkey')
    op.drop_column('readings', 'room_id')

    op.drop_constraint(None, 'users', type_='foreignkey')
    op.drop_column('users', 'room_id')

    op.drop_index('uq_room_dormitory_number', table_name='rooms')
    op.drop_table('rooms')