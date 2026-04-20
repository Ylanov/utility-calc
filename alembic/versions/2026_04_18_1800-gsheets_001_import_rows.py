"""GSheets import buffer table

Revision ID: gsheets_001_import_rows
Revises: sec_001_user_lockout_fields
Create Date: 2026-04-18 18:00:00.000000

Таблица буфер показаний, импортированных из Google Sheets.
Хранит каждую строку с результатом fuzzy-матчинга и статусом
утверждения админом.
"""
from alembic import op
import sqlalchemy as sa


revision = 'gsheets_001_import_rows'
down_revision = 'sec_001_user_lockout_fields'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'gsheets_import_rows',
        sa.Column('id', sa.Integer(), nullable=False, autoincrement=True),

        # Сырые данные из таблицы
        sa.Column('sheet_timestamp', sa.DateTime(), nullable=True),
        sa.Column('raw_fio', sa.String(), nullable=False),
        sa.Column('raw_dormitory', sa.String(), nullable=True),
        sa.Column('raw_room_number', sa.String(), nullable=True),
        sa.Column('raw_hot_water', sa.String(), nullable=True),
        sa.Column('raw_cold_water', sa.String(), nullable=True),

        # Разобранные значения
        sa.Column('hot_water', sa.Numeric(12, 3), nullable=True),
        sa.Column('cold_water', sa.Numeric(12, 3), nullable=True),

        # Результат match
        sa.Column('matched_user_id', sa.Integer(), nullable=True),
        sa.Column('matched_room_id', sa.Integer(), nullable=True),
        sa.Column('match_score', sa.Integer(), server_default='0'),

        # Статус обработки
        sa.Column('status', sa.String(), server_default='pending', nullable=True),
        sa.Column('conflict_reason', sa.Text(), nullable=True),

        # Ссылки
        sa.Column('reading_id', sa.Integer(), nullable=True),
        sa.Column('row_hash', sa.String(32), nullable=False),

        # Метаданные
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('processed_at', sa.DateTime(), nullable=True),
        sa.Column('processed_by_id', sa.Integer(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),

        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('row_hash', name='uq_gsheets_row_hash'),
        sa.ForeignKeyConstraint(['matched_user_id'], ['users.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['matched_room_id'], ['rooms.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['processed_by_id'], ['users.id'], ondelete='SET NULL'),
        # readings — партиционированная таблица, FK на неё в PostgreSQL нельзя,
        # поэтому reading_id не имеет constraint — проверяем вручную в коде.
    )

    op.create_index(
        'idx_gsheets_status_created',
        'gsheets_import_rows',
        ['status', 'created_at'],
    )
    op.create_index(
        'idx_gsheets_matched_user',
        'gsheets_import_rows',
        ['matched_user_id'],
    )
    op.create_index(
        'idx_gsheets_timestamp',
        'gsheets_import_rows',
        ['sheet_timestamp'],
    )
    op.create_index(
        'idx_gsheets_row_hash',
        'gsheets_import_rows',
        ['row_hash'],
    )


def downgrade() -> None:
    op.drop_index('idx_gsheets_row_hash', table_name='gsheets_import_rows')
    op.drop_index('idx_gsheets_timestamp', table_name='gsheets_import_rows')
    op.drop_index('idx_gsheets_matched_user', table_name='gsheets_import_rows')
    op.drop_index('idx_gsheets_status_created', table_name='gsheets_import_rows')
    op.drop_table('gsheets_import_rows')
