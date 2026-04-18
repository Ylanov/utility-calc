"""Scaling indexes for 5-10k users

Revision ID: perf_001_scaling_indexes
Revises: add_002_tariff_effective_from
Create Date: 2026-04-18 14:00:00.000000

Добавляет индексы, критичные для пиковой нагрузки при подаче показаний
20-25 числа месяца (все 10к пользователей одновременно):

1. readings(user_id, is_approved)    — запросы истории конкретного жильца
2. readings(user_id, created_at DESC) — сортировка истории по дате
3. readings(period_id, is_approved, created_at) — выборка за период в админке
4. readings GIN(anomaly_flags)       — фильтрация аномалий без seq scan
5. audit_log(action, created_at DESC) — фильтр журнала по типу действия
6. audit_log(entity_type, created_at DESC) — фильтр журнала по сущности

Все индексы создаются CONCURRENTLY через autocommit_block — не блокируют
таблицу во время миграции, безопасно для прод-развёртывания с живым трафиком.
"""
from alembic import op
import sqlalchemy as sa


revision = 'perf_001_scaling_indexes'
down_revision = 'add_002_tariff_effective_from'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # CREATE INDEX CONCURRENTLY нельзя выполнять внутри транзакции.
    # autocommit_block временно переключает соединение в AUTOCOMMIT.
    with op.get_context().autocommit_block():
        op.execute(sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_reading_user_approved "
            "ON readings (user_id, is_approved)"
        ))
        op.execute(sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_reading_user_created "
            "ON readings (user_id, created_at DESC)"
        ))
        op.execute(sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_reading_period_approved_created "
            "ON readings (period_id, is_approved, created_at DESC)"
        ))
        # GIN-индекс по JSONB anomaly_flags ускоряет фильтр
        # WHERE anomaly_flags @> '{"flag": "HIGH"}' в 10-100 раз.
        op.execute(sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_reading_anomaly_flags_gin "
            "ON readings USING gin (anomaly_flags jsonb_path_ops)"
        ))
        op.execute(sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_action_created "
            "ON audit_log (action, created_at DESC)"
        ))
        op.execute(sa.text(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_entity_created "
            "ON audit_log (entity_type, created_at DESC)"
        ))


def downgrade() -> None:
    with op.get_context().autocommit_block():
        for idx_name in [
            "idx_audit_entity_created",
            "idx_audit_action_created",
            "idx_reading_anomaly_flags_gin",
            "idx_reading_period_approved_created",
            "idx_reading_user_created",
            "idx_reading_user_approved",
        ]:
            op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {idx_name}"))
