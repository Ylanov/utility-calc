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
    op.execute("""
        ALTER TABLE public.readings
        ADD COLUMN IF NOT EXISTS anomaly_score INTEGER DEFAULT 0;
    """)

    # --- Гарантируем наличие колонки во всех partition ---
    op.execute("""
    DO $$
    DECLARE
        r RECORD;
    BEGIN
        FOR r IN
            SELECT inhrelid::regclass AS child
            FROM pg_inherits
            WHERE inhparent = 'public.readings'::regclass
        LOOP
            BEGIN
                EXECUTE format(
                    'ALTER TABLE %s ADD COLUMN anomaly_score INTEGER DEFAULT 0',
                    r.child
                );
            EXCEPTION
                WHEN duplicate_column THEN
                    -- колонка уже существует, ничего не делаем
                    NULL;
            END;
        END LOOP;
    END $$;
    """)


def downgrade() -> None:
    # --- Удаляем колонку из parent ---
    op.execute("""
        ALTER TABLE public.readings
        DROP COLUMN IF EXISTS anomaly_score;
    """)
