"""tariffs_seasonal_002_per_tariff — сезонность на уровне ТАРИФА.

Раньше:
  tariffs_seasonal_001_seed добавлял ДВА глобальных SystemSetting:
    heating_season_active (true/false)
    hot_water_heating_active (true/false)
  Это «один выключатель на всю систему».

Проблема:
  В одном городе может быть несколько общежитий с разными графиками
  отопления (ЦСООР Лидер и ФИЛИ — разные поставщики тепла). Глобальный
  переключатель отключает отопление СРАЗУ ДЛЯ ВСЕХ — нельзя сказать
  «у Лидера сезон с 15.10, у ФИЛИ с 01.11». Админ должен переключать
  вручную и помнить даты.

Решение:
  Сезонные настройки переносятся в сам тариф. Каждый тариф знает:
    - heating_active        — мастер-выключатель статьи «отопление»
    - heating_season_start  — дата начала сезона (год игнорируется, важны MM-DD)
    - heating_season_end    — дата окончания сезона
    - hw_heating_active     — мастер-выключатель «подогрев ГВС»
    - hw_heating_season_start
    - hw_heating_season_end

  Если start = end = NULL → действует круглогодично (active управляет).
  Если start/end заданы → активен только когда сегодняшняя MM-DD в диапазоне.

Глобальные SystemSetting'и сохраняются как EMERGENCY OVERRIDE:
  если SystemSetting.heating_season_active='false' → отключает ВСЕМ
  тарифам отопление (admin может «нажать stop» аварийно).
  Это back-compat и страховка.

Backward compatibility:
  - Дефолты колонок: heating_active=true, hw_heating_active=true,
    даты NULL → круглогодично. Существующее поведение «сезона нет,
    отопление всегда начисляется» сохраняется.
  - Старые SystemSetting'и не трогаются — продолжают работать как
    глобальный outermost-override.
"""
from alembic import op
import sqlalchemy as sa


revision = 'tariffs_seasonal_002_per_tariff'
down_revision = 'tariffs_seasonal_001_seed'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 6 новых колонок. DATE — потому что HTML <input type="date"> и
    # дата-пикеры красивые. Год при сравнении игнорируется (см. метод
    # is_heating_active_now в models.py).
    #
    # server_default не ставим для дат — NULL означает «круглогодично».
    # Для bool'ов ставим true (без изменений в поведении после миграции).
    op.add_column('tariffs', sa.Column(
        'heating_active', sa.Boolean(), nullable=False, server_default=sa.text('true'),
    ))
    op.add_column('tariffs', sa.Column(
        'heating_season_start', sa.Date(), nullable=True,
    ))
    op.add_column('tariffs', sa.Column(
        'heating_season_end', sa.Date(), nullable=True,
    ))
    op.add_column('tariffs', sa.Column(
        'hw_heating_active', sa.Boolean(), nullable=False, server_default=sa.text('true'),
    ))
    op.add_column('tariffs', sa.Column(
        'hw_heating_season_start', sa.Date(), nullable=True,
    ))
    op.add_column('tariffs', sa.Column(
        'hw_heating_season_end', sa.Date(), nullable=True,
    ))


def downgrade() -> None:
    op.drop_column('tariffs', 'hw_heating_season_end')
    op.drop_column('tariffs', 'hw_heating_season_start')
    op.drop_column('tariffs', 'hw_heating_active')
    op.drop_column('tariffs', 'heating_season_end')
    op.drop_column('tariffs', 'heating_season_start')
    op.drop_column('tariffs', 'heating_active')
