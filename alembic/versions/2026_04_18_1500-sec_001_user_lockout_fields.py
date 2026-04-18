"""Add account lockout fields to users

Revision ID: sec_001_user_lockout_fields
Revises: perf_001_scaling_indexes
Create Date: 2026-04-18 15:00:00.000000

Добавляет поля для защиты от brute-force:
- failed_login_count: счётчик неудачных попыток входа
- locked_until: временная блокировка до указанного момента
- last_login_at: время последнего успешного входа (для аудита)

Нужно для подсчёта неверных паролей и блокировки учётки на 15 минут
после 3 неудачных попыток. До этого рейтлимитер 5/60s позволял
перебрать 6-значный числовой пароль за ~3 часа.
"""
from alembic import op
import sqlalchemy as sa


revision = 'sec_001_user_lockout_fields'
down_revision = 'perf_001_scaling_indexes'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default=0 и 'false' — чтобы существующие 10к записей
    # мгновенно получили безопасные значения без долгого UPDATE.
    op.add_column(
        'users',
        sa.Column(
            'failed_login_count',
            sa.Integer(),
            nullable=False,
            server_default='0',
        ),
    )
    op.add_column(
        'users',
        sa.Column('locked_until', sa.DateTime(), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('last_login_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'last_login_at')
    op.drop_column('users', 'locked_until')
    op.drop_column('users', 'failed_login_count')
