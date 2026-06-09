"""room qr_token — постоянный неугадываемый токен квартиры для QR-портала подачи

Revision ID: qr_token_001
Revises: ot_staff_001
Create Date: 2026-06-09 21:00

Анонимный QR-портал по квартире: Room.qr_token (secrets.token_urlsafe(32)) —
ссылка /q/<token>. Nullable: токен выдаётся лениво (при первой генерации QR
в админке). UNIQUE — резолв комнаты по токену O(1), без коллизий.
"""
from alembic import op
import sqlalchemy as sa


revision = "qr_token_001"
down_revision = "ot_staff_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("qr_token", sa.String(), nullable=True))
    op.create_index("ix_rooms_qr_token", "rooms", ["qr_token"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_rooms_qr_token", table_name="rooms")
    op.drop_column("rooms", "qr_token")
