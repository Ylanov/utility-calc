"""Arsenal user management: is_active, last_login, full_name, contact, lockout

Revision ID: arsenal_upg_003
Revises: arsenal_upg_002
Create Date: 2026-04-27 10:00:00.000000

Расширение ArsenalUser для полноценного управления:
  * full_name, phone, email — контакт и ФИО (для идентификации с МОЛ)
  * is_active, deactivated_at, deactivated_by_id, deactivation_reason —
    soft disable вместо delete, чтобы не сломать audit-trail
  * last_login_at, last_login_ip — аналитика
  * failed_login_count, locked_until — защита от перебора

Все существующие пользователи получают is_active=True при upgrade.
"""
from alembic import op
import sqlalchemy as sa


revision = 'arsenal_upg_003'
down_revision = 'arsenal_upg_002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('arsenal_users', sa.Column('full_name', sa.String(), nullable=True))
    op.add_column('arsenal_users', sa.Column('phone', sa.String(32), nullable=True))
    op.add_column('arsenal_users', sa.Column('email', sa.String(), nullable=True))

    op.add_column(
        'arsenal_users',
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
    )
    op.add_column('arsenal_users', sa.Column('deactivated_at', sa.DateTime(), nullable=True))
    op.add_column('arsenal_users', sa.Column('deactivated_by_id', sa.Integer(), nullable=True))
    op.add_column('arsenal_users', sa.Column('deactivation_reason', sa.Text(), nullable=True))
    op.create_foreign_key(
        'fk_arsenal_user_deactivated_by',
        'arsenal_users', 'arsenal_users',
        ['deactivated_by_id'], ['id'],
        ondelete='SET NULL',
    )

    op.add_column('arsenal_users', sa.Column('last_login_at', sa.DateTime(), nullable=True))
    op.add_column('arsenal_users', sa.Column('last_login_ip', sa.String(45), nullable=True))
    op.add_column(
        'arsenal_users',
        sa.Column('failed_login_count', sa.Integer(), nullable=False, server_default='0'),
    )
    op.add_column('arsenal_users', sa.Column('locked_until', sa.DateTime(), nullable=True))

    op.create_index('ix_arsenal_users_is_active', 'arsenal_users', ['is_active'])


def downgrade() -> None:
    op.drop_index('ix_arsenal_users_is_active', table_name='arsenal_users')
    op.drop_column('arsenal_users', 'locked_until')
    op.drop_column('arsenal_users', 'failed_login_count')
    op.drop_column('arsenal_users', 'last_login_ip')
    op.drop_column('arsenal_users', 'last_login_at')
    op.drop_constraint('fk_arsenal_user_deactivated_by', 'arsenal_users', type_='foreignkey')
    op.drop_column('arsenal_users', 'deactivation_reason')
    op.drop_column('arsenal_users', 'deactivated_by_id')
    op.drop_column('arsenal_users', 'deactivated_at')
    op.drop_column('arsenal_users', 'is_active')
    op.drop_column('arsenal_users', 'email')
    op.drop_column('arsenal_users', 'phone')
    op.drop_column('arsenal_users', 'full_name')
