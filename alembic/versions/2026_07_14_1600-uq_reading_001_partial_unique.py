"""uq_reading_001: частичные уникальные индексы «одно утверждённое показание
на жильца в периоде» на каждой партиции readings.

Аудит 2026-07-14 (инцидент «Мороз»): пути подачи создавали ВТОРОЕ утверждённое
показание того же user+period → нулевые дельты, «Объём 0.00» в PDF, двойной учёт.
Глобальный UNIQUE(user_id, period_id) на партиционированной таблице невозможен
(PARTITION BY RANGE(created_at) требует ключ партиционирования в unique), но
партиции годовые (2024..2035 + default, все созданы заранее) — частичный
уникальный индекс на КАЖДОЙ партиции закрывает все случаи, кроме дубля через
границу года (обе записи должны попасть в один год → на практике всегда).

Предикат пропускает служебные записи, законно живущие рядом с подачей:
- черновики (is_approved=false) — сколько угодно;
- замену счётчика (METER_CLOSED + METER_REPLACEMENT в одном периоде);
- разовые начисления при выселении (ONE_TIME_CHARGE*);
- квитанции-сальдо без показаний (MANUAL_RECEIPT).

Каждая партиция — в своём SAVEPOINT: если где-то остался старый дубль,
индекс этой партиции пропускается С ГРОМКИМ WARNING (деплой не валим,
остальные партиции защищаются). Идемпотентно (IF NOT EXISTS).
"""
from alembic import op
import sqlalchemy as sa
import logging

revision = "uq_reading_001"
down_revision = "lk_ai_purge_001"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

INDEX_PREDICATE = """
    is_approved
    AND user_id IS NOT NULL
    AND period_id IS NOT NULL
    AND strpos(coalesce(anomaly_flags, ''), 'METER_') = 0
    AND strpos(coalesce(anomaly_flags, ''), 'ONE_TIME_CHARGE') = 0
    AND strpos(coalesce(anomaly_flags, ''), 'MANUAL_RECEIPT') = 0
"""


def _partitions(bind):
    rows = bind.execute(sa.text(
        "SELECT inhrelid::regclass::text FROM pg_inherits "
        "WHERE inhparent = 'readings'::regclass ORDER BY 1"
    )).scalars().all()
    return list(rows)


def upgrade():
    bind = op.get_bind()
    parts = _partitions(bind)
    if not parts:
        logger.warning("[uq_reading_001] партиции readings не найдены — пропуск")
        return
    created, skipped = [], []
    for part in parts:
        # regclass::text может быть схемо-квалифицированным ('public.readings_2026')
        # — для ИМЕНИ индекса точка недопустима, берём только имя таблицы.
        idx = f"uq_{part.split('.')[-1]}_user_period_appr"
        try:
            with bind.begin_nested():  # SAVEPOINT — провал одной партиции не валит миграцию
                bind.execute(sa.text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {idx} "
                    f"ON {part} (user_id, period_id) WHERE {INDEX_PREDICATE}"
                ))
            created.append(part)
        except Exception as e:  # скорее всего — существующий дубль в партиции
            skipped.append(part)
            logger.warning(
                "[uq_reading_001] %s: индекс НЕ создан (%s). Вероятно, в партиции "
                "остался дубль user+period — найти: SELECT user_id, period_id, "
                "count(*) FROM %s WHERE %s GROUP BY 1,2 HAVING count(*)>1; "
                "вычистить и повторить CREATE UNIQUE INDEX вручную.",
                part, str(e).splitlines()[0] if str(e) else e, part,
                " ".join(INDEX_PREDICATE.split()),
            )
    logger.info("[uq_reading_001] индексы: created=%s skipped=%s", created, skipped)


def downgrade():
    bind = op.get_bind()
    for part in _partitions(bind):
        bind.execute(sa.text(
            f"DROP INDEX IF EXISTS uq_{part.split('.')[-1]}_user_period_appr"
        ))
