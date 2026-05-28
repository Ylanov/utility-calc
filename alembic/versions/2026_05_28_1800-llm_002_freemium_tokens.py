"""llm_002_freemium_tokens — токен-бюджет для Freemium-подписок (L8).

Контекст: GigaChat Freemium даёт фиксированный пакет токенов в месяц
(Lite: 248k, Pro: 38k, Max: 50k) — не «рубли в день».

Добавляем поле monthly_budget_tokens. Если > 0 → используем токен-режим.
Иначе → старый рубль/день (для будущих платных подписок).

Также добавляем monthly_budget_period_start — чтобы считать сколько
потратили с момента обновления подписки (по умолчанию = 1-е число
текущего месяца).
"""
from alembic import op
import sqlalchemy as sa


revision = 'llm_002_freemium_tokens'
down_revision = 'llm_001_settings_calls'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "llm_settings",
        sa.Column(
            "monthly_budget_tokens", sa.Integer(),
            nullable=False, server_default="0",
        ),
    )
    # Дата старта периода (когда обновляются токены подписки). В Freemium
    # Сбер обновляет раз в месяц; админ может уточнить точную дату.
    op.add_column(
        "llm_settings",
        sa.Column("monthly_period_start", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("llm_settings", "monthly_period_start")
    op.drop_column("llm_settings", "monthly_budget_tokens")
