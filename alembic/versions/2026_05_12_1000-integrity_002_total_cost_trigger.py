"""integrity_002 — заменяем CHECK constraint на BEFORE-trigger,
который автоматически синхронизирует total_cost = total_209 + total_205.

Предыдущая миграция integrity_001 поставила CHECK constraint, который
ЛОВИЛ рассогласование, но не лечил его — Python должен был сам корректно
выставлять total_cost при каждом UPDATE/INSERT. Это работало, но
требовало бдительности от разработчиков: легко добавить новый код-пас
(как было с gsheets-promote, который писал total_cost=0) и нарваться на
CHECK error.

Этот шаг — переход к «source of truth в БД»:
  - BEFORE INSERT OR UPDATE триггер сам пересчитывает total_cost из
    total_209 + total_205. Что бы Python ни прислал — БД синхронизирует.
  - CHECK constraint больше не нужен (триггер гарантирует равенство).
  - Существующие сетеры в Python не вредят и могут быть удалены
    постепенно (не атомарно с миграцией).

Это полу-шаг к финальному решению (GENERATED ALWAYS AS STORED column),
которое требует DROP+ADD column → table rewrite на партициях → downtime
несколько минут. Trigger даёт ту же гарантию консистентности БЕЗ
rewrite, поэтому делаем сейчас, GENERATED column — потом, если будет
надо (например, для запрета записи в total_cost из любых клиентов).

Партицированная таблица readings (PARTITION BY RANGE created_at):
  PostgreSQL 13+ автоматически распространяет ROW triggers с parent
  table на все partitions. Тестировалось на PG 15 (наша версия).
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'integrity_002_total_cost_trigger'
down_revision = 'integrity_001_invariant'
branch_labels = None
depends_on = None


SYNC_FUNC = "sync_readings_total_cost"
TRIGGER_NAME = "trg_readings_sync_total_cost"
OLD_CHECK = "chk_readings_total_consistency"


def upgrade() -> None:
    # 1) Создаём (или пересоздаём) функцию-синхронизатор.
    # NEW — это row, который сейчас INSERT/UPDATE-ится. Просто переписываем
    # total_cost суммой компонент. COALESCE — на случай если кто-то прислал
    # NULL в total_209/205 (default-ы в таблице 0.00, но защита не лишняя).
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SYNC_FUNC}()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.total_cost := COALESCE(NEW.total_209, 0)
                            + COALESCE(NEW.total_205, 0);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    # 2) Снимаем CHECK constraint — триггер гарантирует равенство, и
    # дополнительная проверка только мешает (если когда-нибудь захотим
    # явно нарушить инвариант для одной строки, например в тестовом
    # сценарии, CHECK не позволит).
    op.execute(f"""
        ALTER TABLE readings
        DROP CONSTRAINT IF EXISTS {OLD_CHECK}
    """)

    # 3) Auto-heal: если в БД остались legacy-записи с расхождением (CHECK
    # их не должен был пропустить, но если миграция integrity_001 не
    # применялась — на всякий случай). Тот же запрос что был в integrity_001.
    op.execute("""
        UPDATE readings
        SET total_cost = COALESCE(total_209, 0) + COALESCE(total_205, 0)
        WHERE ABS(
            COALESCE(total_cost, 0)
            - COALESCE(total_209, 0)
            - COALESCE(total_205, 0)
        ) > 0.01
    """)

    # 4) Создаём триггер. BEFORE INSERT OR UPDATE — срабатывает ДО записи,
    # позволяет переписать NEW.total_cost. FOR EACH ROW — обязательно для
    # доступа к NEW.
    op.execute(f"""
        CREATE TRIGGER {TRIGGER_NAME}
        BEFORE INSERT OR UPDATE OF total_209, total_205, total_cost
        ON readings
        FOR EACH ROW
        EXECUTE FUNCTION {SYNC_FUNC}()
    """)


def downgrade() -> None:
    # Снимаем триггер и функцию, возвращаем CHECK constraint.
    op.execute(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON readings")
    op.execute(f"DROP FUNCTION IF EXISTS {SYNC_FUNC}()")

    # Восстанавливаем integrity_001 CHECK — без него инвариант не защищён.
    # Сначала auto-heal на случай если в окно «триггер дропнут, но check
    # ещё не создан» успели прийти кривые INSERT/UPDATE.
    op.execute("""
        UPDATE readings
        SET total_cost = COALESCE(total_209, 0) + COALESCE(total_205, 0)
        WHERE ABS(
            COALESCE(total_cost, 0)
            - COALESCE(total_209, 0)
            - COALESCE(total_205, 0)
        ) > 0.01
    """)
    op.execute(f"""
        ALTER TABLE readings
        ADD CONSTRAINT {OLD_CHECK}
        CHECK (
            ABS(
                COALESCE(total_cost, 0)
                - COALESCE(total_209, 0)
                - COALESCE(total_205, 0)
            ) <= 0.01
        )
        NOT VALID
    """)
    op.execute(f"ALTER TABLE readings VALIDATE CONSTRAINT {OLD_CHECK}")
