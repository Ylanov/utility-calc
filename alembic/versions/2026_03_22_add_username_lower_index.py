"""add case-insensitive unique index for username

Revision ID: 2b3c4d5e6f7g
Revises: 1a2b3c4d5e6f
Create Date: 2026-03-22 12:00:00.000000

"""

from alembic import op


# revision identifiers, used by Alembic.
revision = '2b3c4d5e6f7g'
down_revision = '1a2b3c4d5e6f'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ✅ создаём уникальный индекс без учета регистра
    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_user_username_lower
        ON users (LOWER(username));
    """)


def downgrade() -> None:
    # ❌ удаляем индекс
    op.execute("""
        DROP INDEX IF EXISTS uq_user_username_lower;
    """)
