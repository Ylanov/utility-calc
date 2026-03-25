"""add rooms and refactor user + readings

Revision ID: 3c4d5e6f7g8h
Revises: 2b3c4d5e6f7g
Create Date: 2026-03-22 23:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3c4d5e6f7g8h'
down_revision = '2b3c4d5e6f7g'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ======================================================
    # 1. СОЗДАЕМ ТАБЛИЦУ ROOMS
    # ======================================================
    op.create_table(
        'rooms',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('dormitory_name', sa.String(), nullable=True),
        sa.Column('room_number', sa.String(), nullable=True),
        sa.Column('apartment_area', sa.Numeric(10, 2), default=0.00),
        sa.Column('total_room_residents', sa.Integer(), default=1),

        sa.Column('last_hot_water', sa.Numeric(12, 3), default=0.000),
        sa.Column('last_cold_water', sa.Numeric(12, 3), default=0.000),
        sa.Column('last_electricity', sa.Numeric(12, 3), default=0.000),
    )

    # уникальность комнаты
    op.create_index(
        'uq_room_dormitory_number',
        'rooms',
        ['dormitory_name', 'room_number'],
        unique=True
    )

    # ======================================================
    # 2. USER — УБИРАЕМ СТАРОЕ
    # ======================================================
    op.drop_index('idx_user_dormitory_trgm', table_name='users')

    op.drop_column('users', 'dormitory')
    op.drop_column('users', 'apartment_area')
    op.drop_column('users', 'total_room_residents')

    # ======================================================
    # 3. USER — ДОБАВЛЯЕМ ROOM
    # ======================================================
    op.add_column('users', sa.Column('room_id', sa.Integer(), nullable=True))

    op.create_foreign_key(
        'fk_users_room_id',
        'users',
        'rooms',
        ['room_id'],
        ['id'],
    )

    # ======================================================
    # 4. METER READING — ДОБАВЛЯЕМ ROOM_ID
    # ======================================================
    op.execute("""
        ALTER TABLE public.readings
        ADD COLUMN IF NOT EXISTS room_id INTEGER;
    """)

    op.create_foreign_key(
        'fk_readings_room_id',
        'readings',
        'rooms',
        ['room_id'],
        ['id'],
    )

    # ======================================================
    # 5. ДЕЛАЕМ user_id NULLABLE
    # ======================================================
    op.alter_column(
        'readings',
        'user_id',
        existing_type=sa.Integer(),
        nullable=True
    )

    # ======================================================
    # 6. НОВЫЕ ИНДЕКСЫ ДЛЯ ROOM
    # ======================================================
    op.create_index(
        'idx_reading_room_period',
        'readings',
        ['room_id', 'period_id'],
        unique=False
    )

    op.create_index(
        'idx_reading_room_approved',
        'readings',
        ['room_id', 'is_approved'],
        unique=False
    )


def downgrade() -> None:
    # ======================================================
    # 1. УДАЛЯЕМ ИНДЕКСЫ
    # ======================================================
    op.drop_index('idx_reading_room_approved', table_name='readings')
    op.drop_index('idx_reading_room_period', table_name='readings')

    # ======================================================
    # 2. ВОЗВРАЩАЕМ user_id NOT NULL
    # ======================================================
    op.alter_column(
        'readings',
        'user_id',
        existing_type=sa.Integer(),
        nullable=False
    )

    # ======================================================
    # 3. УДАЛЯЕМ room_id ИЗ readings
    # ======================================================
    op.drop_constraint('fk_readings_room_id', 'readings', type_='foreignkey')

    op.execute("""
        ALTER TABLE public.readings
        DROP COLUMN IF EXISTS room_id;
    """)

    # ======================================================
    # 4. USER — УБИРАЕМ ROOM
    # ======================================================
    op.drop_constraint('fk_users_room_id', 'users', type_='foreignkey')
    op.drop_column('users', 'room_id')

    # ======================================================
    # 5. ВОЗВРАЩАЕМ СТАРЫЕ КОЛОНКИ
    # ======================================================
    op.add_column('users', sa.Column('dormitory', sa.String(), nullable=True))
    op.add_column('users', sa.Column('apartment_area', sa.Numeric(10, 2), default=0.00))
    op.add_column('users', sa.Column('total_room_residents', sa.Integer(), default=1))

    op.create_index(
        'idx_user_dormitory_trgm',
        'users',
        ['dormitory'],
        postgresql_using='gin',
        postgresql_ops={"dormitory": "gin_trgm_ops"}
    )

    # ======================================================
    # 6. УДАЛЯЕМ ROOMS
    # ======================================================
    op.drop_index('uq_room_dormitory_number', table_name='rooms')
    op.drop_table('rooms')
