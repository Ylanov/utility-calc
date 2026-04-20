"""App releases table

Revision ID: app_001_releases
Revises: gsheets_001_import_rows
Create Date: 2026-04-20 15:00:00.000000

Таблица для управления версиями мобильного приложения, скачиваемого
напрямую с нашего сервера (минуя Google Play). Админ загружает APK
через панель → клиент проверяет версию при запуске → скачивает обновление.
"""
from alembic import op
import sqlalchemy as sa


revision = 'app_001_releases'
down_revision = 'gsheets_001_import_rows'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'app_releases',
        sa.Column('id', sa.Integer(), nullable=False, autoincrement=True),
        sa.Column('version', sa.String(), nullable=False),
        sa.Column('version_code', sa.Integer(), nullable=False),
        sa.Column('min_required_version_code', sa.Integer(), nullable=True),
        sa.Column('platform', sa.String(), server_default='android', nullable=False),
        sa.Column('file_name', sa.String(), nullable=False),
        sa.Column('file_size', sa.Integer(), nullable=False),
        sa.Column('file_hash', sa.String(), nullable=True),
        sa.Column('release_notes', sa.Text(), nullable=True),
        sa.Column('is_published', sa.Boolean(), server_default='false', nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], ondelete='SET NULL'),
    )

    op.create_index(
        'idx_app_release_platform_published',
        'app_releases',
        ['platform', 'is_published', 'version_code'],
    )
    op.create_index('ix_app_releases_platform', 'app_releases', ['platform'])
    op.create_index('ix_app_releases_is_published', 'app_releases', ['is_published'])
    op.create_index('ix_app_releases_created_at', 'app_releases', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_app_releases_created_at', table_name='app_releases')
    op.drop_index('ix_app_releases_is_published', table_name='app_releases')
    op.drop_index('ix_app_releases_platform', table_name='app_releases')
    op.drop_index('idx_app_release_platform_published', table_name='app_releases')
    op.drop_table('app_releases')
