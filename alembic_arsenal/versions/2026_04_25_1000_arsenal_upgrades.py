"""Arsenal upgrades: rollback, disposal_reasons, inventory, password_reset, low-stock

Revision ID: arsenal_upg_001
Revises: 7e8bc3dc33d2
Create Date: 2026-04-25 10:00:00.000000

Изначально была ошибочно помещена в utility-alembic (head: domain_001_resident_types),
где таблицы `nomenclature` / `documents` не существуют. Перенесена в
alembic_arsenal — цепочка арсенальных миграций.

Содержимое:
  1. nomenclature.min_quantity — порог «низкий остаток» для алертов.
  2. documents: is_reversed / reversed_by_document_id / reverses_document_id /
     disposal_reason_id — трекинг обратных документов и причина списания.
  3. disposal_reasons — справочник причин утилизации (9 типовых причин).
  4. inventories / inventory_items — полноценная инвентаризация.
  5. arsenal_password_reset_tokens — безопасный сброс пароля.
"""
from alembic import op
import sqlalchemy as sa


revision = 'arsenal_upg_001'
down_revision = '7e8bc3dc33d2'
branch_labels = None
depends_on = None


DISPOSAL_REASONS_SEED = [
    ("BREAKDOWN",       "Поломка / неисправность",               "disposal"),
    ("WEAR_OUT",        "Износ / истечение срока эксплуатации",  "disposal"),
    ("DEFECTIVE",       "Заводской брак",                        "disposal"),
    ("LOST",            "Утрата",                                "lost"),
    ("STOLEN",          "Хищение",                               "lost"),
    ("TO_HEADQUARTERS", "Передача в вышестоящее управление",     "external"),
    ("TO_EXTERNAL",     "Передача сторонней организации",        "external"),
    ("COMBAT_LOSS",     "Списание по боевой обстановке",         "disposal"),
    ("OTHER",           "Иная причина",                          "other"),
]


def upgrade() -> None:
    # --- 1. nomenclature.min_quantity ---
    op.add_column(
        'nomenclature',
        sa.Column('min_quantity', sa.Integer(), nullable=False, server_default='0'),
    )

    # --- 2. disposal_reasons (сначала, до FK от documents) ---
    op.create_table(
        'disposal_reasons',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('code', sa.String(32), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False, server_default='disposal'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.UniqueConstraint('code', name='uq_disposal_reason_code'),
    )
    seed = sa.table(
        'disposal_reasons',
        sa.column('code', sa.String),
        sa.column('name', sa.String),
        sa.column('kind', sa.String),
    )
    op.bulk_insert(seed, [
        {'code': c, 'name': n, 'kind': k} for c, n, k in DISPOSAL_REASONS_SEED
    ])

    # --- 3. documents: rollback + disposal reason ---
    op.add_column('documents', sa.Column('is_reversed', sa.Boolean(),
                  nullable=False, server_default=sa.text('false')))
    op.add_column('documents', sa.Column('reversed_by_document_id', sa.Integer(), nullable=True))
    op.add_column('documents', sa.Column('reverses_document_id', sa.Integer(), nullable=True))
    op.add_column('documents', sa.Column('disposal_reason_id', sa.Integer(), nullable=True))
    op.create_index('ix_documents_is_reversed', 'documents', ['is_reversed'])
    op.create_foreign_key('fk_doc_reversed_by', 'documents', 'documents',
                          ['reversed_by_document_id'], ['id'])
    op.create_foreign_key('fk_doc_reverses', 'documents', 'documents',
                          ['reverses_document_id'], ['id'])
    op.create_foreign_key('fk_doc_disposal_reason', 'documents', 'disposal_reasons',
                          ['disposal_reason_id'], ['id'])

    # --- 4. inventory tables ---
    op.create_table(
        'inventories',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('object_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(16), nullable=False, server_default='open'),
        sa.Column('started_by_id', sa.Integer(), nullable=True),
        sa.Column('closed_by_id', sa.Integer(), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('correction_document_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['object_id'], ['accounting_objects.id']),
        sa.ForeignKeyConstraint(['started_by_id'], ['arsenal_users.id']),
        sa.ForeignKeyConstraint(['closed_by_id'], ['arsenal_users.id']),
        sa.ForeignKeyConstraint(['correction_document_id'], ['documents.id']),
    )
    op.create_index('ix_inventory_object_status', 'inventories', ['object_id', 'status'])

    op.create_table(
        'inventory_items',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('inventory_id', sa.Integer(), nullable=False),
        sa.Column('nomenclature_id', sa.Integer(), nullable=False),
        sa.Column('serial_number', sa.String(), nullable=True),
        sa.Column('found_quantity', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('scanned_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('scanned_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['inventory_id'], ['inventories.id']),
        sa.ForeignKeyConstraint(['nomenclature_id'], ['nomenclature.id']),
        sa.ForeignKeyConstraint(['scanned_by_id'], ['arsenal_users.id']),
    )
    op.create_index('ix_inventory_item_scan',
                    'inventory_items', ['inventory_id', 'nomenclature_id'])

    # --- 5. password reset tokens ---
    op.create_table(
        'arsenal_password_reset_tokens',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(128), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.ForeignKeyConstraint(['user_id'], ['arsenal_users.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['arsenal_users.id']),
        sa.UniqueConstraint('token_hash', name='uq_arsenal_pwd_reset_token_hash'),
    )
    op.create_index('ix_arsenal_pwd_reset_user', 'arsenal_password_reset_tokens', ['user_id'])
    op.create_index('ix_arsenal_pwd_reset_expires', 'arsenal_password_reset_tokens', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_arsenal_pwd_reset_expires', table_name='arsenal_password_reset_tokens')
    op.drop_index('ix_arsenal_pwd_reset_user', table_name='arsenal_password_reset_tokens')
    op.drop_table('arsenal_password_reset_tokens')
    op.drop_index('ix_inventory_item_scan', table_name='inventory_items')
    op.drop_table('inventory_items')
    op.drop_index('ix_inventory_object_status', table_name='inventories')
    op.drop_table('inventories')
    op.drop_constraint('fk_doc_disposal_reason', 'documents', type_='foreignkey')
    op.drop_constraint('fk_doc_reverses', 'documents', type_='foreignkey')
    op.drop_constraint('fk_doc_reversed_by', 'documents', type_='foreignkey')
    op.drop_index('ix_documents_is_reversed', table_name='documents')
    op.drop_column('documents', 'disposal_reason_id')
    op.drop_column('documents', 'reverses_document_id')
    op.drop_column('documents', 'reversed_by_document_id')
    op.drop_column('documents', 'is_reversed')
    op.drop_table('disposal_reasons')
    op.drop_column('nomenclature', 'min_quantity')
