"""Add audit_log table and updated_at columns

Revision ID: fix_002_audit_log_updated_at
Revises: fix_001_unique_active_period
Create Date: 2026-04-14 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = 'fix_002_audit_log_updated_at'
down_revision: Union[str, Sequence[str], None] = 'fix_001_unique_active_period'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Новая таблица: Журнал действий администратора
    op.create_table(
        'audit_log',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),

        # УЛУЧШЕНИЕ: nullable=True и ondelete='SET NULL'.
        # Если администратор или пользователь будет удален из БД,
        # журнал аудита не удалится каскадно и не выдаст ошибку.
        # id сбросится в NULL, но поле username (ниже) сохранит его имя в истории.
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('username', sa.String(), nullable=False),

        sa.Column('action', sa.String(), nullable=False),
        sa.Column('entity_type', sa.String(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('details', JSONB(), nullable=True),

        # Используем sa.text('now()') для корректной генерации DEFAULT в PostgreSQL
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
    )

    # Индексы для быстрой фильтрации
    op.create_index('idx_audit_user_id', 'audit_log', ['user_id'])
    op.create_index('idx_audit_entity', 'audit_log', ['entity_type', 'entity_id'])
    op.create_index('idx_audit_action', 'audit_log', ['action'])
    op.create_index('idx_audit_created', 'audit_log', ['created_at'])

    # 2. Добавляем updated_at в существующие таблицы
    # nullable=True чтобы не ломать уже существующие в БД строки
    op.add_column('users', sa.Column('updated_at', sa.DateTime(), nullable=True))
    op.add_column('rooms', sa.Column('updated_at', sa.DateTime(), nullable=True))
    op.add_column('tariffs', sa.Column('updated_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    # Удаляем колонки (порядок не важен)
    op.drop_column('tariffs', 'updated_at')
    op.drop_column('rooms', 'updated_at')
    op.drop_column('users', 'updated_at')

    # ИСПРАВЛЕНИЕ СИНТАКСИСА: Для удаления индексов в Alembic нужно явно указывать table_name
    op.drop_index('idx_audit_created', table_name='audit_log')
    op.drop_index('idx_audit_action', table_name='audit_log')
    op.drop_index('idx_audit_entity', table_name='audit_log')
    op.drop_index('idx_audit_user_id', table_name='audit_log')

    # Удаляем таблицу
    op.drop_table('audit_log')