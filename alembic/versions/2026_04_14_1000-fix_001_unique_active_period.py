"""Add unique partial index for single active billing period

ИСПРАВЛЕНИЕ: Без этого индекса в таблице periods может быть несколько строк
с is_active = TRUE одновременно. Это происходит при:
- Параллельных запросах (race condition при открытии периода)
- Ручной правке БД
- Баге в коде (если забыли деактивировать старый период)

Если два периода активны — ВСЕ расчёты (bulk_approve, close_period, auto-generate)
используют select(BillingPeriod).where(is_active) и берут .first(),
что возвращает непредсказуемый из двух периодов.

Partial unique index гарантирует на уровне PostgreSQL, что максимум одна строка
может иметь is_active = TRUE. Любая попытка создать вторую вернёт UniqueViolation.

Revision ID: fix_001_unique_active_period
Revises: 6f7g8h9i0j1k
Create Date: 2026-04-14 10:00:00.000000
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'fix_001_unique_active_period'
down_revision = '6f7g8h9i0j1k'  # Указывает на последнюю миграцию (security_and_race_condition_fix)
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Partial unique index: разрешает только одну строку с is_active = TRUE.
    # Строки с is_active = FALSE не попадают в индекс и могут быть в любом количестве.

    # ВАЖНО: Если в базе УЖЕ есть два активных периода, создание индекса упадет с ошибкой.
    # Поэтому перед созданием индекса безопасно деактивируем все периоды, кроме самого свежего.
    op.execute("""
               UPDATE periods
               SET is_active = FALSE
               WHERE is_active = TRUE
                 AND id NOT IN (SELECT id
                                FROM periods
                                WHERE is_active = TRUE
                                ORDER BY created_at DESC
                   LIMIT 1
                   );
               """)

    op.execute("""
               CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_period
                   ON periods (is_active)
                   WHERE is_active = TRUE;
               """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_one_active_period;")