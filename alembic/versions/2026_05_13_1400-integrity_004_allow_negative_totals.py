"""integrity_004 — разрешить отрицательные total_209/205/cost (переплата).

В integrity_003 CHECK constraints на total_209/total_205/total_cost
были вида `>= 0 AND <= 10_000_000`. Это блокировало legitimate use case:
когда у жильца переплата превышает начисления, total получается
отрицательный (остаток средств). Например manual_receipt для Глобы:
  total_209 = 707.70 (фикс-часть) - 7091.90 (переплата) = -6384.20

При INSERT/UPDATE такого reading БД возвращала IntegrityError, FastAPI
отдавал 500. Админ не мог создать квитанцию с учётом переплаты.

Фикс: разрешить отрицательные значения в диапазоне [-10_000_000, 10_000_000].
debt_209/debt_205 остаются >= 0 (это абсолютные суммы из 1С).
overpayment_* тоже >= 0 (тоже абсолют).
hot_water/cold_water/electricity тоже >= 0 (физически отрицательное
потребление невозможно).

Только total_209/205/cost могут быть отрицательными — это финальный
итог с учётом всех долгов/переплат.
"""
from alembic import op


revision = 'integrity_004_neg_totals'
# Линейная цепочка: integrity_003 → debts_002 → debts_003 → integrity_004.
# Раньше down_revision указывал на integrity_003, но debts_002/003 уже
# применены поверх integrity_003 → alembic выдавал «Multiple head revisions».
#
# ID сокращён до 24 символов: alembic_version.version_num — VARCHAR(32),
# полное имя 'integrity_004_allow_negative_totals' (36 символов) не
# помещалось — UPDATE падал StringDataRightTruncationError.
down_revision = 'debts_003_applied_state'
branch_labels = None
depends_on = None


# Старые constraints (со >= 0), которые нужно заменить
_OLD = [
    "chk_readings_total_209_bounds",
    "chk_readings_total_205_bounds",
    "chk_readings_total_cost_bounds",
]

# Новые constraints — позволяют отрицательные значения (переплата)
_NEW = [
    ("chk_readings_total_209_bounds",
     "total_209 IS NULL OR (total_209 >= -10000000 AND total_209 <= 10000000)"),
    ("chk_readings_total_205_bounds",
     "total_205 IS NULL OR (total_205 >= -10000000 AND total_205 <= 10000000)"),
    ("chk_readings_total_cost_bounds",
     "total_cost IS NULL OR (total_cost >= -10000000 AND total_cost <= 10000000)"),
]


def upgrade() -> None:
    # Дропаем старые constraints
    for name in _OLD:
        op.execute(f"ALTER TABLE readings DROP CONSTRAINT IF EXISTS {name}")
    # Создаём новые с расширенным диапазоном
    for name, expr in _NEW:
        op.execute(f"""
            ALTER TABLE readings
            ADD CONSTRAINT {name}
            CHECK ({expr})
            NOT VALID
        """)


def downgrade() -> None:
    # Откатываем обратно к >= 0 (из integrity_003)
    for name in _OLD:
        op.execute(f"ALTER TABLE readings DROP CONSTRAINT IF EXISTS {name}")
    _ORIGINAL = [
        ("chk_readings_total_209_bounds",
         "total_209 IS NULL OR (total_209 >= 0 AND total_209 <= 10000000)"),
        ("chk_readings_total_205_bounds",
         "total_205 IS NULL OR (total_205 >= 0 AND total_205 <= 10000000)"),
        ("chk_readings_total_cost_bounds",
         "total_cost IS NULL OR (total_cost >= 0 AND total_cost <= 10000000)"),
    ]
    for name, expr in _ORIGINAL:
        op.execute(f"""
            ALTER TABLE readings
            ADD CONSTRAINT {name}
            CHECK ({expr})
            NOT VALID
        """)
