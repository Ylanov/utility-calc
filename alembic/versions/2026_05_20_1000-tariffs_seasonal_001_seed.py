"""tariffs_seasonal_001_seed — сезонные переключатели как SystemSetting.

Бизнес-задача: некоторые статьи тарифа (отопление, подогрев ГВС) —
сезонные. Раньше админ редактировал тариф «убрать heating летом» и
возвращал поле обратно осенью. Это шумно (видно как тарифный коммит),
ошибаемо (легко забыть), и не работает для подогрева ГВС во время
кратковременной профилактики ТЭЦ.

Решение — два глобальных булевых SystemSetting'а:
  heating_season_active       (true/false, дефолт true)
  hot_water_heating_active    (true/false, дефолт true)

При false соответствующая ставка зануляется в calculate_utilities()
(см. app/modules/utility/services/calculations.py). Тариф остаётся
неизменным, переключение — за 1 секунду, без перерасчёта старых
квитанций (для них есть отдельный «Перерасчёт периода»).

Эта миграция сидит дефолтные значения в БД, чтобы:
  - state был виден через SELECT в БД (а не «default из кода»);
  - audit-логи переключений писались на UPDATE существующей записи.
Если строки уже есть (admin успел нажать «Применить») — НЕ перезаписываем.
"""
from alembic import op
import sqlalchemy as sa


revision = 'tariffs_seasonal_001_seed'
down_revision = 'token_001_version'
branch_labels = None
depends_on = None


# Используем те же тексты, что в SETTINGS module — single source of truth
# в системе всё-таки код, но для повторяемого DDL дублируем здесь.
_SEED = (
    ("heating_season_active", "true",
     "Отопительный сезон открыт (true/false). При false — cost_heating всегда 0."),
    ("hot_water_heating_active", "true",
     "Подогрев ГВС включён (true/false). При false — cost_hot_water считается "
     "как если бы вода была холодной (только water_supply, без water_heating). "
     "Полезно во время летней профилактики ТЭЦ."),
)


def upgrade() -> None:
    # ON CONFLICT DO NOTHING — если админ уже нажал PUT /api/settings/seasonal
    # до применения миграции, его значения сохраняем.
    conn = op.get_bind()
    for key, value, description in _SEED:
        conn.execute(
            sa.text("""
                INSERT INTO system_settings (key, value, description)
                VALUES (:key, :value, :description)
                ON CONFLICT (key) DO NOTHING
            """),
            {"key": key, "value": value, "description": description},
        )


def downgrade() -> None:
    # Удаляем только наши ключи. Если админ их вручную правил — потеряет
    # значение, но это семантика downgrade'а: возвращаем БД к состоянию
    # до этой миграции.
    conn = op.get_bind()
    for key, _v, _d in _SEED:
        conn.execute(
            sa.text("DELETE FROM system_settings WHERE key = :key"),
            {"key": key},
        )
