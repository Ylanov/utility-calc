"""Resident types, billing modes, per-capita tariff, room assignment history

Revision ID: domain_001_resident_types
Revises: tariff_001_room_tariff
Create Date: 2026-04-24 12:00:00.000000

Доменная доработка под реальную картину общежитий:
  * жильцы бывают семейные и одиночки (холостяки);
  * семьи платят ПО СЧЁТЧИКАМ (как раньше);
  * холостяки, живущие вместе в одной комнате, платят за КОЙКО-МЕСТО —
    фиксированная сумма из тарифа, независимо от показаний;
  * жильцы переезжают между комнатами / увольняются — нужна история.

Что меняется в схеме:
  1. users.resident_type ('family' | 'single')   default 'family'
  2. users.billing_mode  ('by_meter' | 'per_capita')  default 'by_meter'
  3. tariffs.per_capita_amount Numeric(10,2)     default 0
  4. Таблица room_assignments — история переездов
  5. Сидируем room_assignments по текущим User.room_id (открытые записи) —
     чтобы существующие жильцы сразу имели «активное» проживание.
"""
from alembic import op
import sqlalchemy as sa


revision = 'domain_001_resident_types'
down_revision = 'tariff_001_room_tariff'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) Жильцы: тип и режим оплаты
    op.add_column(
        'users',
        sa.Column('resident_type', sa.String(16),
                  nullable=False, server_default='family'),
    )
    op.add_column(
        'users',
        sa.Column('billing_mode', sa.String(16),
                  nullable=False, server_default='by_meter'),
    )
    op.create_index('idx_users_resident_type', 'users', ['resident_type'])
    op.create_index('idx_users_billing_mode', 'users', ['billing_mode'])

    # 2) Тариф: фикс. сумма за койко-место
    op.add_column(
        'tariffs',
        sa.Column('per_capita_amount', sa.Numeric(10, 2),
                  nullable=False, server_default='0.00'),
    )

    # 3) История переездов
    op.create_table(
        'room_assignments',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('room_id', sa.Integer(), nullable=False),
        sa.Column('moved_in_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('moved_out_at', sa.DateTime(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['room_id'], ['rooms.id']),
    )
    op.create_index('idx_assignment_user_active',
                    'room_assignments', ['user_id', 'moved_out_at'])
    op.create_index('idx_assignment_room_dates',
                    'room_assignments', ['room_id', 'moved_in_at', 'moved_out_at'])

    # 4) Сид: на каждого живого жильца с привязкой к комнате — открытая запись
    # Дату ставим NOW() (точную дату въезда не знаем, главное — иметь актив).
    op.execute("""
        INSERT INTO room_assignments (user_id, room_id, moved_in_at, note)
        SELECT id, room_id, NOW(), 'auto-imported on migration'
          FROM users
         WHERE room_id IS NOT NULL
           AND COALESCE(is_deleted, false) = false
    """)


def downgrade() -> None:
    op.drop_index('idx_assignment_room_dates', table_name='room_assignments')
    op.drop_index('idx_assignment_user_active', table_name='room_assignments')
    op.drop_table('room_assignments')
    op.drop_column('tariffs', 'per_capita_amount')
    op.drop_index('idx_users_billing_mode', table_name='users')
    op.drop_index('idx_users_resident_type', table_name='users')
    op.drop_column('users', 'billing_mode')
    op.drop_column('users', 'resident_type')
