"""integrity_001 — CHECK constraint: total_cost = total_209 + total_205

Revision ID: integrity_001_invariant
Revises: perf_002_gsheets_reading_null
Create Date: 2026-05-10 15:00:00.000000

Проблема: код в нескольких местах сохранял MeterReading где
`total_cost ≠ total_209 + total_205`. Один пример — UPDATE-ветка
client_readings.py до фикса may 2026 (setattr-цикл перезаписывал
total_cost). Без CHECK constraint в БД такие рассогласованные записи
просто оседали и портили SUM на дашборде.

Делаем в два шага:
  1. Авто-фикс существующих legacy-записей: где разница больше копейки —
     приводим total_cost к (total_209 + total_205). Это безопасно:
     total_209/205 — суммы по двум счетам (КБК), их разница и есть
     корректный total_cost. Плюс перед миграцией админ должен запустить
     audit_calculations.py и убедиться что нет аномалий.
  2. Добавляем NOT VALID constraint, потом VALIDATE — это позволяет
     добавить ограничение без долгого блокирующего скана таблицы.

Downgrade: просто DROP CONSTRAINT. Данные не страдают.
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'integrity_001_invariant'
down_revision = 'perf_002_gsheets_reading_null'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Auto-heal: где total_cost отличается от total_209+total_205 более
    # чем на 1 копейку — корректируем. Допустимая дельта 0.01 учитывает
    # rounding погрешность (наш quantize_money даёт 2 знака, расхождения
    # быть не должно, но защита от будущих float-impact).
    op.execute("""
        UPDATE readings
        SET total_cost = COALESCE(total_209, 0) + COALESCE(total_205, 0)
        WHERE ABS(
            COALESCE(total_cost, 0)
            - COALESCE(total_209, 0)
            - COALESCE(total_205, 0)
        ) > 0.01
    """)

    # 2. Constraint без блокирующего скана: NOT VALID + потом VALIDATE.
    # NOT VALID — блокирует только новые INSERT/UPDATE, существующие данные
    # не сканирует (мгновенно).
    op.execute("""
        ALTER TABLE readings
        ADD CONSTRAINT chk_readings_total_consistency
        CHECK (
            ABS(
                COALESCE(total_cost, 0)
                - COALESCE(total_209, 0)
                - COALESCE(total_205, 0)
            ) <= 0.01
        )
        NOT VALID
    """)

    # VALIDATE — отдельной командой. Сканирует всю таблицу, но без
    # SHARE-блокировки, поэтому конкурентные INSERT/UPDATE продолжаются.
    op.execute("ALTER TABLE readings VALIDATE CONSTRAINT chk_readings_total_consistency")


def downgrade() -> None:
    op.execute("ALTER TABLE readings DROP CONSTRAINT IF EXISTS chk_readings_total_consistency")
