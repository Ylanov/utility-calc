"""Analyzer settings + anomaly dismissals

Revision ID: analyzer_001_settings
Revises: gsheets_002_aliases
Create Date: 2026-04-22 10:00:00.000000

Унифицированный «Центр анализа»:
  * analyzer_settings — все пороги (FUZZY_THRESHOLD, MAD-multiplier и т.д.)
    в БД, редактируются админом без релиза.
  * anomaly_dismissals — пометки «эта аномалия для жильца X — false-positive»,
    self-learning слой над детектором.

Также сидируем ВСЕ существующие захардкоженные пороги — чтобы текущее
поведение не изменилось при первом деплое.
"""
from alembic import op
import sqlalchemy as sa


revision = 'analyzer_001_settings'
down_revision = 'gsheets_002_aliases'
branch_labels = None
depends_on = None


# Сид с текущими значениями. Если что-то меняется в коде по умолчанию —
# здесь же бампим, либо новой миграцией. См. analyzer_config.py для использования.
SETTINGS_SEED = [
    # ---- GSheets matcher ----
    ("gsheets.fuzzy_threshold", "78", "int", "gsheets",
     "Минимальный fuzzy-score для статуса pending. Ниже — unmatched.",
     "50", "100", True),
    ("gsheets.auto_approve_threshold", "95", "int", "gsheets",
     "Score ≥ это и комната совпадает → auto_approve без участия админа.",
     "80", "100", True),
    ("gsheets.ambiguity_band", "2", "int", "gsheets",
     "Если несколько кандидатов в пределах N очков от лучшего — conflict.",
     "0", "10", True),

    # ---- Anomaly detector v2 ----
    ("anomaly.mad_multiplier", "4", "int", "anomaly",
     "Множитель MAD: SPIKE если delta > median + N×MAD.",
     "2", "10", True),
    ("anomaly.soft_spike_factor", "2", "float", "anomaly",
     "HIGH_X если delta > N × median.", "1.2", "5", True),
    ("anomaly.peer_factor", "3", "float", "anomaly",
     "HIGH_VS_PEERS если расход > N × среднего по группе.", "1.5", "10", True),
    ("anomaly.high_per_person_cold", "12", "float", "anomaly",
     "Лимит ХВС м³ на 1 проживающего в месяц.", "5", "30", True),

    # ---- Approval gate ----
    ("approve.score_threshold", "80", "int", "approve",
     "Bulk-approve пропускает только показания со score < N.", "0", "100", True),

    # ---- New rules (toggleable) ----
    ("rule.round_number.enabled", "true", "bool", "rules",
     "Подозрение на округление: целое число без дробной части.",
     None, None, True),
    ("rule.hot_gt_cold.enabled", "true", "bool", "rules",
     "Аномалия: ГВС > ХВС (физически нетипично, ХВС обычно идёт на готовку, питание).",
     None, None, True),
    ("rule.copy_neighbor.enabled", "true", "bool", "rules",
     "Подозрение что списали у соседа: значения совпадают с точностью до 0.001.",
     None, None, True),
    ("rule.gap_recovery.enabled", "true", "bool", "rules",
     "Большая подача после долгой паузы (3+ месяца без подач).",
     None, None, True),
    ("rule.copy_neighbor.epsilon", "0.001", "float", "rules",
     "Допуск равенства для COPY_NEIGHBOR.", "0", "1", True),

    # ---- Debt import ----
    ("debt.fuzzy_threshold", "88", "int", "debt",
     "Минимальный fuzzy-score для матча в импорте долгов из 1С.",
     "70", "100", True),
]


def upgrade() -> None:
    op.create_table(
        'analyzer_settings',
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
        sa.ForeignKeyConstraint(['updated_by_id'], ['users.id']),
    )
    op.create_index('idx_analyzer_setting_category',
                    'analyzer_settings', ['category'])

    op.create_table(
        'anomaly_dismissals',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('flag_code', sa.String(48), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['created_by_id'], ['users.id']),
    )
    op.create_index('uq_anomaly_dismissal',
                    'anomaly_dismissals', ['user_id', 'flag_code'], unique=True)
    op.create_index('idx_anomaly_dismissal_flag',
                    'anomaly_dismissals', ['flag_code'])

    # Seed defaults — текущее поведение сохраняется 1:1.
    settings_table = sa.table(
        'analyzer_settings',
        sa.column('key', sa.String),
        sa.column('value', sa.String),
        sa.column('value_type', sa.String),
        sa.column('category', sa.String),
        sa.column('description', sa.Text),
        sa.column('min_value', sa.String),
        sa.column('max_value', sa.String),
        sa.column('is_enabled', sa.Boolean),
    )
    op.bulk_insert(settings_table, [
        {
            'key': k, 'value': v, 'value_type': vt, 'category': cat,
            'description': desc, 'min_value': mn, 'max_value': mx,
            'is_enabled': en,
        }
        for (k, v, vt, cat, desc, mn, mx, en) in SETTINGS_SEED
    ])


def downgrade() -> None:
    op.drop_index('idx_anomaly_dismissal_flag', table_name='anomaly_dismissals')
    op.drop_index('uq_anomaly_dismissal', table_name='anomaly_dismissals')
    op.drop_table('anomaly_dismissals')
    op.drop_index('idx_analyzer_setting_category', table_name='analyzer_settings')
    op.drop_table('analyzer_settings')
