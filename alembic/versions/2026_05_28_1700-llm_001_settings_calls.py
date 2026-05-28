"""llm_001_settings_calls — пилот ИИ-помощника (GigaChat Lite).

L1 (28.05.2026): база для подключения LLM-провайдера:

  llm_settings (singleton): provider / model_name / token_encrypted /
    enabled / daily_budget_rub / base_url / disabled_until / disabled_reason.
    Токен хранится зашифрованным через Fernet (ключ из env LLM_SECRET_KEY).

  llm_calls: каждый вызов LLM — purpose, prompt_chars, response_chars,
    cost_rub, latency_ms, success, related_type/related_id (для линковки
    с error_log / users / tickets).

  ALTER error_log: добавляем ai_analysis JSONB + ai_analyzed_at + ai_model
    — для фонового AI-анализа ошибок (L5).

Архитектура: GigaChat сейчас, в будущем — vLLM/ollama локально (тот же
OpenAI-совместимый API). Меняется только провайдер в llm_settings.

Безопасность:
- Токен НИКОГДА не возвращается из API в открытом виде; для UI
  показываем только последние 4 символа («****abcd»).
- Доступ к /api/admin/llm/* только для role='admin'.
- LLM не получает прямой WRITE-доступ в БД — только чтение + текст.
- При превышении daily_budget провайдер автоматически выключается
  до полуночи (disabled_until = next midnight UTC).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'llm_001_settings_calls'
down_revision = 'errors_001_error_log'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────
    # 1. llm_settings — конфиг провайдера (singleton: всегда id=1).
    # ─────────────────────────────────────────────────────────────────
    op.create_table(
        "llm_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        # gigachat_lite | gigachat_pro | gigachat_max | local_vllm | disabled
        sa.Column("provider", sa.String(length=32),
                  nullable=False, server_default="disabled"),
        # GigaChat / GigaChat-Pro / GigaChat-Max / Qwen2.5-14B / ...
        sa.Column("model_name", sa.String(length=64),
                  nullable=False, server_default="GigaChat"),
        # Fernet-зашифрованный токен. NULL = не настроен.
        sa.Column("token_encrypted", sa.Text(), nullable=True),
        # Дополнительный base_url для local_vllm / ollama (OpenAI-compat).
        sa.Column("base_url", sa.String(length=256), nullable=True),
        # Главный выключатель.
        sa.Column("enabled", sa.Boolean(),
                  nullable=False, server_default=sa.text("false")),
        # Бюджет — рубли в день. По истечении — провайдер выключается.
        sa.Column("daily_budget_rub", sa.Numeric(10, 2),
                  nullable=False, server_default="50"),
        # Авто-блок до этого времени (превышен бюджет, hard error...).
        sa.Column("disabled_until", sa.DateTime(), nullable=True),
        sa.Column("disabled_reason", sa.String(length=200), nullable=True),
        # Аудит.
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by_id", sa.Integer(), nullable=True),
    )
    # Singleton: вставляем сразу одну строку с id=1, чтобы UPDATE-ом
    # обновлять её, а не INSERT-OR-UPDATE дёргать.
    op.execute(
        "INSERT INTO llm_settings (id, provider, model_name, enabled) "
        "VALUES (1, 'disabled', 'GigaChat', false)"
    )

    # ─────────────────────────────────────────────────────────────────
    # 2. llm_calls — audit каждого вызова LLM.
    # ─────────────────────────────────────────────────────────────────
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "occurred_at", sa.DateTime(),
            nullable=False, server_default=sa.text("(now() AT TIME ZONE 'utc')"),
            index=True,
        ),
        # error_analysis | user_summary | daily_briefing | ticket_classify | test | manual
        sa.Column("purpose", sa.String(length=64), nullable=False, index=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model_name", sa.String(length=64), nullable=False),

        # Размеры запроса/ответа в символах (приблизительная мера).
        sa.Column("prompt_chars", sa.Integer(), nullable=False),
        sa.Column("response_chars", sa.Integer(), nullable=True),
        # Если провайдер вернул использование токенов — пишем.
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("response_tokens", sa.Integer(), nullable=True),

        # Расчётная стоимость в рублях. Считается через таблицу
        # PRICING_PER_1K в коде, по prompt_tokens+response_tokens.
        sa.Column("cost_rub", sa.Numeric(10, 4), nullable=True, index=True),

        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("success", sa.Boolean(),
                  nullable=False, server_default=sa.text("false")),
        sa.Column("error", sa.Text(), nullable=True),

        # Связанная сущность — для линковки в UI (error_log/user/ticket).
        sa.Column("related_type", sa.String(length=32), nullable=True, index=True),
        sa.Column("related_id", sa.Integer(), nullable=True, index=True),
    )
    op.create_index(
        "idx_llm_calls_purpose_day",
        "llm_calls",
        ["purpose", "occurred_at"],
    )

    # ─────────────────────────────────────────────────────────────────
    # 3. ALTER error_log: ai_analysis для L5.
    # ─────────────────────────────────────────────────────────────────
    op.add_column("error_log", sa.Column("ai_analysis", JSONB, nullable=True))
    op.add_column("error_log", sa.Column("ai_analyzed_at", sa.DateTime(), nullable=True))
    op.add_column("error_log", sa.Column("ai_model", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("error_log", "ai_model")
    op.drop_column("error_log", "ai_analyzed_at")
    op.drop_column("error_log", "ai_analysis")

    op.drop_index("idx_llm_calls_purpose_day", table_name="llm_calls")
    op.drop_table("llm_calls")
    op.drop_table("llm_settings")
