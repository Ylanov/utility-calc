"""meter_fmt_002_strict — обновить SystemSetting на 5_3_strict.

ИМЯ РЕВИЗИИ намеренно короткое (≤32 символа). Исходное
'meter_format_002_update_to_5_3_strict' (37 chars) переполняло
alembic_version.version_num VARCHAR(32) → CI ловил
StringDataRightTruncationError на UPDATE alembic_version.

В мае 2026 формат ввода был ужесточён: вместо «5 цифр без точки»
теперь обязательный 5+3 (8 цифр). См. коммит feat(meters): жёсткий
формат 5+3 (8 цифр) для подачи показаний.

Проблема: старые SystemSetting'и meter_format_hint / meter_example_hot /
meter_instructions содержали инструкцию «Запишите только ПЕРВЫЕ 5 цифр»
— и UI её показывал, противореча новому формату.

Эта миграция переписывает все три ключа на новые значения. Если их
не было — создаются. Если были — обновляются (ON CONFLICT DO UPDATE).
"""
from alembic import op
import sqlalchemy as sa


revision = 'meter_fmt_002_strict'
down_revision = 'tariffs_type_001_family_singles'
branch_labels = None
depends_on = None


_NEW_INSTRUCTIONS = (
    "ВСЕГДА вводите все 8 цифр счётчика: 5 цифр до точки + 3 после. "
    "Если на счётчике значение короткое (например «1.4») — допишите "
    "ведущие нули: «00001.400». Это стандарт счётчиков воды в РФ. "
    "Пример: «01433.887»."
)


def upgrade() -> None:
    conn = op.get_bind()
    rows = [
        ("meter_format_hint", "5_3_strict", "Формат ввода счётчиков (жильцу)"),
        ("meter_example_hot", "01433.887", "Пример hot для жильца"),
        ("meter_instructions", _NEW_INSTRUCTIONS, "Текст-инструкция жильцу"),
    ]
    for key, value, description in rows:
        conn.execute(
            sa.text("""
                INSERT INTO system_settings (key, value, description)
                VALUES (:key, :value, :description)
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    description = EXCLUDED.description
            """),
            {"key": key, "value": value, "description": description},
        )


def downgrade() -> None:
    # Возвращаем старые «5_no_decimal» значения для отката.
    conn = op.get_bind()
    rows = [
        ("meter_format_hint", "5_no_decimal", "Формат ввода счётчиков (жильцу)"),
        ("meter_example_hot", "01433", "Пример hot для жильца"),
        ("meter_instructions",
         "Запишите только ПЕРВЫЕ 5 цифр счётчика (целая часть). "
         "Дробные цифры после точки — НЕ нужны. "
         "Пример: на счётчике «01433.887» вводите 01433 или 1433.",
         "Текст-инструкция жильцу"),
    ]
    for key, value, description in rows:
        conn.execute(
            sa.text("""
                INSERT INTO system_settings (key, value, description)
                VALUES (:key, :value, :description)
                ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    description = EXCLUDED.description
            """),
            {"key": key, "value": value, "description": description},
        )
