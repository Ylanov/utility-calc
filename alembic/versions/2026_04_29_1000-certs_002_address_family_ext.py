"""Cert feature v2: registration_address, lives_alone, extended family fields

Revision ID: certs_002_address_family
Revises: certs_001_profile_family
Create Date: 2026-04-29 10:00:00.000000

Расширение волны «Заказ справок»:
  * users.registration_address — адрес прописки по паспорту (отдельно от
    адреса комнаты по договору найма).
  * users.lives_alone — флаг «проживаю один», альтернатива обязательному
    списку членов семьи при заказе справки.
  * family_members.arrival_date — дата прибытия (вселения). Идёт в
    таблицу «Проживающие» справки-выписки.
  * family_members.registration_type — permanent | temporary (по месту
    жительства / по месту пребывания).
  * family_members.relation_to_head — свободный текст, отношение к
    нанимателю как в домовой книге («сын», «жена», «мать»).

Все поля nullable/с дефолтом, чтобы существующие жильцы не сломались —
при первом заказе справки жилец доёпросится и проставит значения.
"""
from alembic import op
import sqlalchemy as sa


revision = 'certs_002_address_family'
down_revision = 'certs_001_profile_family'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # users
    op.add_column(
        'users',
        sa.Column('registration_address', sa.String(500), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column(
            'lives_alone',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
    )

    # family_members
    op.add_column(
        'family_members',
        sa.Column('arrival_date', sa.Date(), nullable=True),
    )
    op.add_column(
        'family_members',
        sa.Column('registration_type', sa.String(20), nullable=True),
    )
    op.add_column(
        'family_members',
        sa.Column('relation_to_head', sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('family_members', 'relation_to_head')
    op.drop_column('family_members', 'registration_type')
    op.drop_column('family_members', 'arrival_date')
    op.drop_column('users', 'lives_alone')
    op.drop_column('users', 'registration_address')
