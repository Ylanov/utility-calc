"""Scaling indexes for 5-10k users

Revision ID: perf_001_scaling_indexes
Revises: add_002_tariff_effective_from
Create Date: 2026-04-18 14:00:00.000000

Добавляет индексы, критичные для пиковой нагрузки при подаче показаний
20-25 числа месяца (все 10к пользователей одновременно):

1. readings(user_id, is_approved)            — история конкретного жильца
2. readings(user_id, created_at DESC)        — сортировка истории по дате
3. readings(period_id, is_approved, created_at) — выборка за период в админке
4. audit_log(action, created_at DESC)        — фильтр журнала по типу действия
5. audit_log(entity_type, created_at DESC)   — фильтр журнала по сущности

ВАЖНО про readings:
- Таблица readings партиционирована (RANGE по created_at).
- PostgreSQL НЕ поддерживает CREATE INDEX CONCURRENTLY на партиционированных
  таблицах. Нужно обычный CREATE INDEX на РОДИТЕЛЬСКОЙ таблице — PostgreSQL
  сам создаст индексы на всех дочерних партициях.
- На пустой/малонаполненной БД это мгновенно.
- На проде с 1М+ записей это заблокирует readings на ~5-10 секунд —
  деплой лучше проводить в окне минимальной активности (ночью).

audit_log НЕ партиционирована — там используем CONCURRENTLY для безопасности.
"""
from alembic import op
import sqlalchemy as sa


revision = 'perf_001_scaling_indexes'
down_revision = 'add_002_tariff_effective_from'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ===============================================================
    # Индексы на readings (партиционированная таблица).
    # Без CONCURRENTLY — иначе PostgreSQL выбрасывает
    # "cannot create index on partitioned table concurrently".
    # Эти CREATE'ы выполняются внутри обычной транзакции Alembic.
    # ===============================================================
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_reading_user_approved "
        "ON readings (user_id, is_approved)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_reading_user_created "
        "ON readings (user_id, created_at DESC)"
    ))
    op.execute(sa.text(
        "CREATE INDEX IF NOT EXISTS idx_reading_period_approved_created "
        "ON readings (period_id, is_approved, created_at DESC)"
    ))

    # ===============================================================
    # Индексы на audit_log (обычная непартиционированная таблица).
    # Здесь можно использовать CONCURRENTLY, чтобы не блокировать запись в журнал.
    # autocommit_block переключает соединение в AUTOCOMMIT временно.
    # ===============================================================
    with op.get_context().autocommit_block():
        op.execute(sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_action_created "
            "ON audit_log (action, created_at DESC)"
        ))
        op.execute(sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_entity_created "
            "ON audit_log (entity_type, created_at DESC)"
        ))


def downgrade() -> None:
    # Сначала audit_log (concurrently), потом readings (обычный DROP).
    with op.get_context().autocommit_block():
        op.execute(sa.text("DROP INDEX CONCURRENTLY IF EXISTS idx_audit_entity_created"))
        op.execute(sa.text("DROP INDEX CONCURRENTLY IF EXISTS idx_audit_action_created"))

    op.execute(sa.text("DROP INDEX IF EXISTS idx_reading_period_approved_created"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_reading_user_created"))
    op.execute(sa.text("DROP INDEX IF EXISTS idx_reading_user_approved"))
