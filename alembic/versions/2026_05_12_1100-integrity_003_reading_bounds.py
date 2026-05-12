"""integrity_003 — CHECK constraints на разумные пороги в readings.

Защита от повторения бага мая 2026, когда из-за пропущенной точки в
Google Sheets-импорте поле hot_water получало значение 1427957 (вместо
1427.957), и итоговый total_cost вырастал до сотен миллионов рублей.
SUM по дашборду показал 1.48 МЛРД ₽ заряжено за апрель — последствия
устраняли скриптом cleanup_anomaly_readings.py.

Reading_validators.py уже проверяет такие значения в Python ДО INSERT.
Но если кто-то добавит новый код-пас, минующий validate_meter_reading()
(или просто прямой UPDATE из миграции/скрипта) — БД должна сама
отказать. Это defense-in-depth, не замена Python-валидации.

Пороги (margin x10 от реалистичных, чтобы не задеть legacy
DATA_OVERFLOW_RESET baselines, у которых иногда стоит ~999999):
  - hot_water, cold_water:   0..100 000 м³    (реалистично 0..10 000)
  - electricity:             0..1 000 000 кВтч (реалистично 0..50 000)
  - total_cost/209/205:      0..10 000 000 ₽   (реалистично 0..100 000)
  - anomaly_score:           0..100            (по дизайну percent)

Применение: NOT VALID без VALIDATE. Это:
  - блокирует НОВЫЕ INSERT/UPDATE которые нарушают границы (главная цель)
  - НЕ сканирует существующие данные (мгновенная миграция, нет блокировок)
  - НЕ упадёт даже если в БД есть legacy-записи нарушающие границы
    (например baseline=999999999 от очень старых импортов)

Если позже захотим VALIDATE existing data — отдельной миграцией, после
очистки legacy через cleanup_anomaly_readings.py.

Партиционированная readings (RANGE created_at): PostgreSQL применяет
ADD CONSTRAINT на parent ко всем partitions автоматически (включая
будущие — новая партиция наследует все constraints parent'а).
"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'integrity_003_reading_bounds'
down_revision = 'integrity_002_total_cost_trigger'
branch_labels = None
depends_on = None


# (constraint_name, sql_expression) — единый список, чтобы upgrade/downgrade
# держали один и тот же набор и не разошлись при правках.
BOUNDS = [
    ("chk_readings_hot_water_bounds",
     "hot_water IS NULL OR (hot_water >= 0 AND hot_water <= 100000)"),
    ("chk_readings_cold_water_bounds",
     "cold_water IS NULL OR (cold_water >= 0 AND cold_water <= 100000)"),
    ("chk_readings_electricity_bounds",
     "electricity IS NULL OR (electricity >= 0 AND electricity <= 1000000)"),
    ("chk_readings_total_209_bounds",
     "total_209 IS NULL OR (total_209 >= 0 AND total_209 <= 10000000)"),
    ("chk_readings_total_205_bounds",
     "total_205 IS NULL OR (total_205 >= 0 AND total_205 <= 10000000)"),
    ("chk_readings_total_cost_bounds",
     "total_cost IS NULL OR (total_cost >= 0 AND total_cost <= 10000000)"),
    ("chk_readings_anomaly_score_bounds",
     "anomaly_score IS NULL OR (anomaly_score >= 0 AND anomaly_score <= 100)"),
]


def upgrade() -> None:
    for name, expr in BOUNDS:
        op.execute(f"""
            ALTER TABLE readings
            ADD CONSTRAINT {name}
            CHECK ({expr})
            NOT VALID
        """)


def downgrade() -> None:
    for name, _ in BOUNDS:
        op.execute(f"ALTER TABLE readings DROP CONSTRAINT IF EXISTS {name}")
