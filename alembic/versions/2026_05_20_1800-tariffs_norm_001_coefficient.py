"""tariffs_norm_001_coefficient — коэффициент-множитель к нормативам.

Бизнес-задача (запрос мая 2026):
  Когда жилец не подаёт показания 4-й месяц подряд — система должна
  начислять не по «среднему за прошлые месяцы», а по нормативу с
  повышающим коэффициентом (по умолчанию ×3). Это санкция за
  длительное игнорирование подачи.

  Сейчас в Tariff есть hw_norm_per_capita, cw_norm_per_capita,
  el_norm_per_capita — нормативы (м³ или кВт·ч / чел / месяц). Но
  множитель один — 1.0 без возможности изменить.

  Добавляем поле norm_coefficient (default 3.0) — множитель именно для
  «long-term defaulter» сценария. На основной расчёт (когда счётчика
  нет, has_X_meter=False) НЕ влияет — он по-прежнему использует
  norm_per_capita × residents без коэффициента (это нейтральное
  начисление, не санкция).

  Применяется только в billing.close_current_period(), когда жилец
  не подал N+ месяцев — см. отдельный коммит auto-billing.

Backward-compat:
  Default 3.0 — типовая санкция в РФ ЖКХ-расчётах. Если админ хочет
  отключить эскалацию — ставит 1.0.
"""
from alembic import op
import sqlalchemy as sa


revision = 'tariffs_norm_001_coefficient'
down_revision = 'tariffs_seasonal_002_per_tariff'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('tariffs', sa.Column(
        'norm_coefficient', sa.Numeric(5, 2),
        nullable=False, server_default='3.00',
    ))


def downgrade() -> None:
    op.drop_column('tariffs', 'norm_coefficient')
