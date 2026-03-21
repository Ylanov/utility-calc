# app/core/db_utils.py


def add_column_partitioned(table: str, column_sql: str) -> str:
    """
    Генерирует SQL для добавления колонки в partitioned таблицу PostgreSQL.

    Использует ALTER TABLE для родительской таблицы.
    Подходит для случаев, когда структура синхронизирована.

    Пример:
        add_column_partitioned("public.readings", "anomaly_score INTEGER DEFAULT 0")
    """
    return f"""
    ALTER TABLE {table}
    ADD COLUMN IF NOT EXISTS {column_sql};
    """


def add_column_partitioned_safe(table: str, column_sql: str) -> str:
    """
    Генерирует SQL для безопасного добавления колонки во все partition.

    Используется, если есть риск рассинхронизации (как у тебя было).
    Добавляет колонку:
    1. В parent таблицу
    2. Во все дочерние partition

    Пример:
        add_column_partitioned_safe("public.readings", "anomaly_score INTEGER DEFAULT 0")
    """
    return f"""
    ALTER TABLE {table}
    ADD COLUMN IF NOT EXISTS {column_sql};

    DO $$
    DECLARE
        r RECORD;
    BEGIN
        FOR r IN
            SELECT inhrelid::regclass AS child
            FROM pg_inherits
            WHERE inhparent = '{table}'::regclass
        LOOP
            BEGIN
                EXECUTE format(
                    'ALTER TABLE %s ADD COLUMN {column_sql}',
                    r.child
                );
            EXCEPTION
                WHEN duplicate_column THEN
                    NULL;
            END;
        END LOOP;
    END $$;
    """


def drop_column_partitioned(table: str, column_name: str) -> str:
    """
    Генерирует SQL для удаления колонки из partitioned таблицы.

    Удаляет только из parent (PostgreSQL сам обработает children).
    """
    return f"""
    ALTER TABLE {table}
    DROP COLUMN IF EXISTS {column_name};
    """