"""tariffs_type_001_family_singles — тип тарифа (семейный / холостяки).

Бизнес-контекст (запрос мая 2026):
  В общежитиях встречаются «коммунальные квартиры» где живут 2-3 ХОЛОСТЯКА
  (каждый сам по себе, не семья). Каждый платит отдельную квитанцию по
  per_capita_amount тарифа. Счётчики физически не разделимы — поэтому
  у этих жильцов billing_mode='per_capita' и они не подают показания.

  Сейчас все тарифы — один и тот же ('ЦСООР Лидер', 'ФИЛИ') с
  per_capita_amount внутри. Но логически тариф может быть РАЗНЫЙ
  для семейной квартиры и для квартиры с холостяками.

  Решение: добавить tariff_type (family / singles), чтобы:
    1) Админ при создании тарифа явно говорит назначение.
    2) Селектор тарифа подсвечивает singles-тарифы (другой цвет).
    3) Сводный отчёт «Жильцы → Холостяки» использует этот признак.

  Поведение расчёта не меняется — тариф остаётся тем же набором ставок.
  tariff_type — это метка для UI и отчётов.

Backward-compat:
  Все существующие тарифы получают 'family' (так было раньше).
  Когда админ создаёт новый «singles»-тариф — отмечает в UI.
"""
from alembic import op
import sqlalchemy as sa


revision = 'tariffs_type_001_family_singles'
down_revision = 'tariffs_norm_001_coefficient'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tariffs', sa.Column(
        'tariff_type', sa.String(20),
        nullable=False, server_default='family',
    ))


def downgrade() -> None:
    op.drop_column('tariffs', 'tariff_type')
