"""pg_stat_statements — расширение для анализа медленных/частых запросов.

Основа для прицельной оптимизации под рост нагрузки: видно топ SQL по времени
и частоте → точечные индексы и борьба с N+1 вместо гадания. САМ СБОР статистики
включает shared_preload_libraries=pg_stat_statements в docker-compose (требует
рестарта postgres); эта миграция лишь создаёт queryable-вью в БД ЖКХ.
IF NOT EXISTS — идемпотентно, безопасно при повторном прогоне.

Требует суперюзера БД — миграции выполняются под POSTGRES_USER (владелец
кластера), право есть.

Топ-20 самых тяжёлых запросов после накопления статистики:
  SELECT query, calls, total_exec_time, mean_exec_time, rows
  FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20;
Сброс статистики:  SELECT pg_stat_statements_reset();
"""
from alembic import op


revision = 'pg_stat_statements_001'
down_revision = 'contracts_001_dedupe'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS pg_stat_statements")
