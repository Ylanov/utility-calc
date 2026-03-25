"""add anomaly_score to readings

Revision ID: 1a2b3c4d5e6f
Revises: dd2c767d6f06
Create Date: 2026-03-21 21:30:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '1a2b3c4d5e6f'
down_revision = 'dd2c767d6f06'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Добавляем колонку в parent (основную таблицу) ---
    # В PostgreSQL 11+ добавление колонки в главную таблицу
    # автоматически и мгновенно пробрасывает её во все дочерние партиции.
    op.execute("""
        ALTER TABLE public.readings
        ADD COLUMN IF NOT EXISTS anomaly_score INTEGER DEFAULT 0;
    """)


def downgrade() -> None:
    # --- Удаляем колонку из parent ---
    # Аналогично, удаление из родительской таблицы каскадно удалит её из всех партиций
    op.execute("""
        ALTER TABLE public.readings
        DROP COLUMN IF EXISTS anomaly_score;
    """)