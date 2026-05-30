"""llm_003_call_text_and_prompt — текст вызовов ИИ + кастомный системный промпт.

Чтобы админ видел, ЧТО ИИ сделал (текст брифинга/анализа/триажа) и мог
редактировать системный промпт:
- LLMCall.prompt_text / response_text — что ушло в ИИ и что вернулось (для
  отчёта «что проверил / что нашёл»).
- LLMSetting.system_prompt — кастомная добавка к встроенному SYSTEM_BASE.
"""
from alembic import op
import sqlalchemy as sa


revision = 'llm_003_call_text_and_prompt'
down_revision = 'meters_002_room_meter_config'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("prompt_text", sa.Text(), nullable=True))
    op.add_column("llm_calls", sa.Column("response_text", sa.Text(), nullable=True))
    op.add_column("llm_settings", sa.Column("system_prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_settings", "system_prompt")
    op.drop_column("llm_calls", "response_text")
    op.drop_column("llm_calls", "prompt_text")
