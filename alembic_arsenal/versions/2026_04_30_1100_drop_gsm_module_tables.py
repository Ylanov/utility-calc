"""Drop GSM module tables (module removed from platform)

Revision ID: arsenal_upg_004_drop_gsm
Revises: arsenal_upg_003
Create Date: 2026-04-30 11:00:00.000000

Модуль ГСМ полностью удалён из платформы (apr 2026): код модуля
(app/modules/gsm), фронтенд (static/gsm_*.html, static/js/gsm.js),
nginx-locations `/api/gsm/`, env-настройки GSM_DB_NAME / GSM_DATABASE_URL_*
больше не используются.

Таблицы создавались миграцией 74cc2ed1171b в этой же базе (arsenal_db),
эта миграция их дропает.

DROP TABLE ... CASCADE — снимает FK / индексы автоматически. IF EXISTS —
идемпотентность (если миграция уже катилась, повторный upgrade не упадёт;
если у кого-то была отдельная gsm_db без этих таблиц в arsenal_db, тоже OK).

Откат:
    downgrade() пересоздаёт таблицы по описанию из 74cc2ed1171b. Данные,
    конечно, не восстанавливаются — restore из бэкапа отдельная история.
"""
from alembic import op
import sqlalchemy as sa


revision = 'arsenal_upg_004_drop_gsm'
down_revision = 'arsenal_upg_003'
branch_labels = None
depends_on = None


# Порядок дропа важен только если бы мы НЕ использовали CASCADE.
# Для надёжности перечисляем в обратном порядке зависимостей.
_GSM_TABLES = (
    "gsm_document_items",
    "gsm_documents",
    "gsm_fuel_registry",
    "gsm_users",
    "gsm_nomenclature",
    "gsm_accounting_objects",
)


def upgrade() -> None:
    for tbl in _GSM_TABLES:
        op.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')


def downgrade() -> None:
    # Воссоздание таблиц на случай отката. Структура снята с миграции
    # 74cc2ed1171b — изменений в этих таблицах с момента создания не
    # делалось, поэтому одна-в-одну.
    op.create_table(
        'gsm_accounting_objects',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('parent_id', sa.Integer(), nullable=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('obj_type', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['parent_id'], ['gsm_accounting_objects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_gsm_accounting_objects_id'), 'gsm_accounting_objects', ['id'], unique=False)

    op.create_table(
        'gsm_nomenclature',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(), nullable=True),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('category', sa.String(), nullable=True),
        sa.Column('is_packaged', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_gsm_nomenclature_code'), 'gsm_nomenclature', ['code'], unique=False)
    op.create_index(op.f('ix_gsm_nomenclature_id'), 'gsm_nomenclature', ['id'], unique=False)

    op.create_table(
        'gsm_users',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(), nullable=False),
        sa.Column('hashed_password', sa.String(), nullable=False),
        sa.Column('role', sa.String(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username'),
    )
    op.create_index(op.f('ix_gsm_users_id'), 'gsm_users', ['id'], unique=False)

    op.create_table(
        'gsm_fuel_registry',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nomenclature_id', sa.Integer(), nullable=False),
        sa.Column('batch_number', sa.String(), nullable=False),
        sa.Column('density', sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column('current_object_id', sa.Integer(), nullable=True),
        sa.Column('status', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.Numeric(precision=15, scale=3), nullable=True),
        sa.ForeignKeyConstraint(['current_object_id'], ['gsm_accounting_objects.id'], ),
        sa.ForeignKeyConstraint(['nomenclature_id'], ['gsm_nomenclature.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_gsm_fuel_registry_id'), 'gsm_fuel_registry', ['id'], unique=False)

    op.create_table(
        'gsm_documents',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('doc_number', sa.String(), nullable=True),
        sa.Column('operation_type', sa.String(), nullable=False),
        sa.Column('operation_date', sa.DateTime(), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=True),
        sa.Column('target_id', sa.Integer(), nullable=True),
        sa.Column('author_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['author_id'], ['gsm_users.id'], ),
        sa.ForeignKeyConstraint(['source_id'], ['gsm_accounting_objects.id'], ),
        sa.ForeignKeyConstraint(['target_id'], ['gsm_accounting_objects.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_gsm_documents_id'), 'gsm_documents', ['id'], unique=False)

    op.create_table(
        'gsm_document_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.Integer(), nullable=False),
        sa.Column('nomenclature_id', sa.Integer(), nullable=False),
        sa.Column('weapon_id', sa.Integer(), nullable=True),
        sa.Column('quantity', sa.Numeric(precision=15, scale=3), nullable=True),
        sa.ForeignKeyConstraint(['document_id'], ['gsm_documents.id'], ),
        sa.ForeignKeyConstraint(['nomenclature_id'], ['gsm_nomenclature.id'], ),
        sa.ForeignKeyConstraint(['weapon_id'], ['gsm_fuel_registry.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_gsm_document_items_id'), 'gsm_document_items', ['id'], unique=False)
