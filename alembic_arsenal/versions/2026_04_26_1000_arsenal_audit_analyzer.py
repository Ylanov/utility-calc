"""Arsenal audit log + analyzer settings + anomaly flags

Revision ID: arsenal_upg_002
Revises: arsenal_upg_001
Create Date: 2026-04-26 10:00:00.000000

Три таблицы:
  1. arsenal_audit_log — «кто когда что сделал» (аудит операций).
  2. arsenal_analyzer_settings — пороги правил «Центра анализа» (редактируются админом).
  3. arsenal_anomaly_flags — результаты работы анализатора (найденные нарушения).

Сидируются базовые пороги 6 правил.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'arsenal_upg_002'
down_revision = 'arsenal_upg_001'
branch_labels = None
depends_on = None


ANALYZER_SETTINGS_SEED = [
    # --- Дубли серийников ---
    ("rule.duplicate_serial.enabled", "true", "bool", "duplicate",
     "Обнаруживать один серийный номер в нескольких местах (активных записях WeaponRegistry).",
     None, None, True),

    # --- Застой остатков ---
    ("rule.stale_stock.enabled", "true", "bool", "stock",
     "Имущество без движения более N месяцев — кандидат на проверку.",
     None, None, True),
    ("rule.stale_stock.months", "24", "int", "stock",
     "Порог «застоя» в месяцах.", "6", "120", True),

    # --- Подозрительный всплеск (фрод) ---
    ("rule.suspicious_burst.enabled", "true", "bool", "fraud",
     "Один пользователь списал/передал больше N единиц за 24 часа — подозрительно.",
     None, None, True),
    ("rule.suspicious_burst.threshold_per_day", "20", "int", "fraud",
     "Сколько движений за сутки считается подозрительным.", "5", "500", True),

    # --- Серийник-призрак: есть в DocumentItem, но нет в WeaponRegistry ---
    ("rule.ghost_serial.enabled", "true", "bool", "data_integrity",
     "Серийный номер упоминается в документах, но отсутствует в реестре — баг данных.",
     None, None, True),

    # --- Партия с нулевым quantity — должна была быть удалена ---
    ("rule.zero_batch.enabled", "true", "bool", "data_integrity",
     "Партия с quantity<=0 ещё числится как активная (status=1) — ошибка логики списания.",
     None, None, True),

    # --- Непринятое после отправки (висящий transit) ---
    ("rule.overdue_shipment.enabled", "true", "bool", "operations",
     "«Отправка» без последующего «Прием» за N дней — имущество зависло в пути.",
     None, None, True),
    ("rule.overdue_shipment.days", "14", "int", "operations",
     "Дней после Отправки, после которых нет Приёма.", "1", "180", True),
]


def upgrade() -> None:
    # --- 1. arsenal_audit_log ---
    op.create_table(
        'arsenal_audit_log',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('username', sa.String(), nullable=False),
        sa.Column('action', sa.String(), nullable=False),
        sa.Column('entity_type', sa.String(), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('details', JSONB(), nullable=True),
        sa.Column('ip_address', sa.String(45), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.ForeignKeyConstraint(['user_id'], ['arsenal_users.id'], ondelete='SET NULL'),
    )
    op.create_index('ix_arsenal_audit_created', 'arsenal_audit_log', ['created_at'])
    op.create_index('ix_arsenal_audit_user_time',
                    'arsenal_audit_log', ['user_id', 'created_at'])
    op.create_index('ix_arsenal_audit_entity',
                    'arsenal_audit_log', ['entity_type', 'entity_id'])

    # --- 2. arsenal_analyzer_settings ---
    op.create_table(
        'arsenal_analyzer_settings',
        sa.Column('key', sa.String(64), primary_key=True),
        sa.Column('value', sa.String(), nullable=False),
        sa.Column('value_type', sa.String(16), nullable=False),
        sa.Column('category', sa.String(32), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('min_value', sa.String(), nullable=True),
        sa.Column('max_value', sa.String(), nullable=True),
        sa.Column('is_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('updated_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['updated_by_id'], ['arsenal_users.id']),
    )
    op.create_index('ix_arsenal_analyzer_category',
                    'arsenal_analyzer_settings', ['category'])

    seed = sa.table(
        'arsenal_analyzer_settings',
        sa.column('key', sa.String),
        sa.column('value', sa.String),
        sa.column('value_type', sa.String),
        sa.column('category', sa.String),
        sa.column('description', sa.Text),
        sa.column('min_value', sa.String),
        sa.column('max_value', sa.String),
        sa.column('is_enabled', sa.Boolean),
    )
    op.bulk_insert(seed, [
        {
            'key': k, 'value': v, 'value_type': vt, 'category': cat,
            'description': desc, 'min_value': mn, 'max_value': mx,
            'is_enabled': en,
        }
        for k, v, vt, cat, desc, mn, mx, en in ANALYZER_SETTINGS_SEED
    ])

    # --- 3. arsenal_anomaly_flags ---
    op.create_table(
        'arsenal_anomaly_flags',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('rule_code', sa.String(48), nullable=False),
        sa.Column('severity', sa.String(16), nullable=False, server_default='warning'),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('details', JSONB(), nullable=True),
        sa.Column('entity_type', sa.String(32), nullable=True),
        sa.Column('entity_id', sa.Integer(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('last_seen_at', sa.DateTime(), nullable=False, server_default=sa.text('NOW()')),
        sa.Column('dismissed_at', sa.DateTime(), nullable=True),
        sa.Column('dismissed_by_id', sa.Integer(), nullable=True),
        sa.Column('dismiss_reason', sa.Text(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['dismissed_by_id'], ['arsenal_users.id']),
        sa.UniqueConstraint('rule_code', 'entity_type', 'entity_id',
                            name='uix_anomaly_rule_entity'),
    )
    op.create_index('ix_anomaly_rule', 'arsenal_anomaly_flags', ['rule_code'])
    op.create_index('ix_anomaly_last_seen', 'arsenal_anomaly_flags', ['last_seen_at'])
    op.create_index('ix_anomaly_active',
                    'arsenal_anomaly_flags',
                    ['rule_code', 'dismissed_at', 'resolved_at'])


def downgrade() -> None:
    op.drop_index('ix_anomaly_active', table_name='arsenal_anomaly_flags')
    op.drop_index('ix_anomaly_last_seen', table_name='arsenal_anomaly_flags')
    op.drop_index('ix_anomaly_rule', table_name='arsenal_anomaly_flags')
    op.drop_table('arsenal_anomaly_flags')
    op.drop_index('ix_arsenal_analyzer_category', table_name='arsenal_analyzer_settings')
    op.drop_table('arsenal_analyzer_settings')
    op.drop_index('ix_arsenal_audit_entity', table_name='arsenal_audit_log')
    op.drop_index('ix_arsenal_audit_user_time', table_name='arsenal_audit_log')
    op.drop_index('ix_arsenal_audit_created', table_name='arsenal_audit_log')
    op.drop_table('arsenal_audit_log')
