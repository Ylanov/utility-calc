"""Drop users.telegram_id (Telegram integration removed)

Revision ID: cleanup_001_drop_telegram
Revises: certs_002_address_family
Create Date: 2026-04-30 10:00:00.000000

Telegram Mini App был удалён из платформы целиком: код модуля
(app/modules/telegram), фронтенд (static/tg_app.*), env-настройка
TELEGRAM_BOT_TOKEN и колонка users.telegram_id больше не используются.

Эта миграция дропает колонку и её unique-индекс.

Откат:
    downgrade() восстанавливает колонку и индекс — но привязки telegram_id
    к жильцам уже потеряны. Если откатывать, привязку придётся делать заново.
"""
from alembic import op
import sqlalchemy as sa


revision = 'cleanup_001_drop_telegram'
down_revision = 'certs_002_address_family'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(op.f('ix_users_telegram_id'), table_name='users')
    op.drop_column('users', 'telegram_id')


def downgrade() -> None:
    op.add_column('users', sa.Column('telegram_id', sa.String(), nullable=True))
    op.create_index(op.f('ix_users_telegram_id'), 'users', ['telegram_id'], unique=True)
