"""Compound partial indexes on readings for hot recalc/bulk-receipt paths

Revision ID: perf_001_readings_compound
Revises: cleanup_001_drop_telegram
Create Date: 2026-04-30 10:10:00.000000

Тяжёлые задачи (start_bulk_receipt_generation, _recalc_run, detect_anomalies_task)
ищут «предыдущее approved-показание» по комнате (или по паре user+room) с
сортировкой по created_at. Без составного индекса PG делал bitmap heap scan +
in-memory sort на десятках тысяч строк readings.

Partial WHERE is_approved экономит размер индекса (черновики не нужны для этих
запросов) и ускоряет именно горячий путь.

CONCURRENTLY — не блокирует таблицу при создании, важно для prod где readings
постоянно пишется. Поэтому миграция помечается transactional=False
(см. env.py / alembic.ini, у нас уже стандартный online режим).
"""
from alembic import op


revision = 'perf_001_readings_compound'
down_revision = 'cleanup_001_drop_telegram'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Используется в start_bulk_receipt_generation и detect_anomalies_task:
    # WHERE room_id IN (...) AND is_approved ORDER BY created_at DESC
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_readings_room_approved_created "
        "ON readings (room_id, created_at) "
        "WHERE is_approved = TRUE"
    )

    # Используется в _recalc_run: WHERE user_id IN (...) AND room_id IN (...)
    # AND is_approved ORDER BY created_at
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_readings_user_room_approved_created "
        "ON readings (user_id, room_id, created_at) "
        "WHERE is_approved = TRUE"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_readings_user_room_approved_created")
    op.execute("DROP INDEX IF EXISTS ix_readings_room_approved_created")
