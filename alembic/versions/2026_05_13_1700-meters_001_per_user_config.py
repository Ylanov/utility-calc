"""meters_001 — конфигурация счётчиков на жильце + нормативы в тарифе.

Бизнес-задача: разные жильцы имеют разный набор счётчиков:
  - кто-то подаёт только ГВС+ХВС (нет электросчётчика)
  - кто-то все три
  - кто-то ничего не подаёт (отказался / нет в комнате)

Раньше система требовала все три показания у каждого. Если жилец не
подавал электричество, анализатор флагил MISSING_ELECT, и админ
вручную закрывал каждую такую квитанцию.

Этот этап:
  1. На жильца — флаги наличия счётчиков (has_hw_meter / has_cw_meter
     / has_el_meter). По умолчанию все True (старое поведение).
  2. На тариф — нормативы на 1 человека в месяц для случая когда
     счётчика нет: hw_norm_per_capita, cw_norm_per_capita,
     el_norm_per_capita. По умолчанию 0 (значит «нет норматива → 0
     потребление»).

Логика в коде (отдельные коммиты):
  - При has_X_meter=False анализатор НЕ флагит «не подал X».
  - В calculate_utilities: если счётчика нет — потребление считается
    как `norm_per_capita × residents_count` (если norm > 0, иначе 0).
  - В мобильном UI: поле input для отсутствующего счётчика скрыто.
"""
from alembic import op
import sqlalchemy as sa


revision = 'meters_001_per_user_config'
down_revision = 'integrity_004_neg_totals'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # User: флаги наличия счётчиков. server_default=true чтобы для
    # существующих жильцов поведение НЕ менялось (как будто счётчики
    # есть у всех — старая логика).
    op.add_column(
        'users',
        sa.Column('has_hw_meter', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )
    op.add_column(
        'users',
        sa.Column('has_cw_meter', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )
    op.add_column(
        'users',
        sa.Column('has_el_meter', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    )

    # Tariff: нормативы для случая когда счётчика нет.
    # Numeric(10, 3) — м³ или кВт·ч на 1 человека в месяц.
    op.add_column(
        'tariffs',
        sa.Column('hw_norm_per_capita', sa.Numeric(10, 3), server_default='0.0', nullable=False),
    )
    op.add_column(
        'tariffs',
        sa.Column('cw_norm_per_capita', sa.Numeric(10, 3), server_default='0.0', nullable=False),
    )
    op.add_column(
        'tariffs',
        sa.Column('el_norm_per_capita', sa.Numeric(10, 3), server_default='0.0', nullable=False),
    )


def downgrade() -> None:
    op.drop_column('tariffs', 'el_norm_per_capita')
    op.drop_column('tariffs', 'cw_norm_per_capita')
    op.drop_column('tariffs', 'hw_norm_per_capita')
    op.drop_column('users', 'has_el_meter')
    op.drop_column('users', 'has_cw_meter')
    op.drop_column('users', 'has_hw_meter')
