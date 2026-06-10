"""room qr_password_hash — пароль QR-портала квартиры

Revision ID: qr_pass_001
Revises: qr_token_001
Create Date: 2026-06-10 12:00

Второй фактор к qr_token: QR-наклейку могут сфотографировать посторонние
(гость, сосед) — пароль знает только жилец. NULL = не установлен, портал
при первом входе просит придумать (модалка установки). Хеш — argon2
(pwd_context из app.core.auth). Сброс: админ или перевыпуск токена.
"""
from alembic import op
import sqlalchemy as sa


revision = "qr_pass_001"
down_revision = "qr_token_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("qr_password_hash", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("rooms", "qr_password_hash")
