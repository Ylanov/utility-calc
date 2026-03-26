"""add tariff_id to users

Revision ID: 6f7g8h9i0j1k
Revises: 5e6f7g8h9i0j
Create Date: 2026-03-26 13:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '6f7g8h9i0j1k'
down_revision = '5e6f7g8h9i0j'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ======================================================
    # ДОБАВЛЯЕМ tariff_id В users
    # ======================================================
    op.add_column(
        'users',
        sa.Column('tariff_id', sa.Integer(), nullable=True)
    )

    # ======================================================
    # ДОБАВЛЯЕМ FOREIGN KEY
    # ======================================================
    op.create_foreign_key(
        'fk_users_tariff_id',
        'users',
        'tariffs',
        ['tariff_id'],
        ['id'],
    )


def downgrade() -> None:
    # ======================================================
    # УДАЛЯЕМ FOREIGN KEY
    # ======================================================
    op.drop_constraint('fk_users_tariff_id', 'users', type_='foreignkey')

    # ======================================================
    # УДАЛЯЕМ КОЛОНКУ
    # ======================================================
    op.drop_column('users', 'tariff_id')