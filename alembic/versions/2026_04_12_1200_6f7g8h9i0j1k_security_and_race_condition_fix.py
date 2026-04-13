"""add partial index on readings and security improvements

Revision ID: 6f7g8h9i0j1k
Revises: 5e6f7g8h9i0j
Create Date: 2026-04-12 12:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6f7g8h9i0j1k'
down_revision = '5e6f7g8h9i0j'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Индекс для поиска черновиков
    # ВАЖНО: Мы убрали UNIQUE и CONCURRENTLY, так как PostgreSQL
    # не поддерживает их создание таким образом на партицированных таблицах.
    op.execute("""
        CREATE INDEX IF NOT EXISTS uix_reading_room_period_draft
        ON readings (room_id, period_id)
        WHERE is_approved = false;
    """)

    # 2. Индекс на anomaly_flags
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_reading_anomaly_flags_notnull
        ON readings (anomaly_flags)
        WHERE anomaly_flags IS NOT NULL;
    """)

    # 3. Индекс на readings.user_id
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_reading_user_id
        ON readings (user_id)
        WHERE user_id IS NOT NULL;
    """)


def downgrade() -> None:
    # УДАЛЯЕМ ИНДЕКСЫ В ОБРАТНОМ ПОРЯДКЕ
    op.execute("DROP INDEX IF EXISTS idx_reading_user_id;")
    op.execute("DROP INDEX IF EXISTS idx_reading_anomaly_flags_notnull;")
    op.execute("DROP INDEX IF EXISTS uix_reading_room_period_draft;")