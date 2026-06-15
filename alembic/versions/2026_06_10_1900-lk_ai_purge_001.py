"""вычистка ЛК жильцов и ИИ-пилота — drop мёртвых таблиц и колонок

Revision ID: lk_ai_purge_001
Revises: qr_pass_001
Create Date: 2026-06-10 19:00

Решение пользователя 2026-06-10: личные кабинеты жильцов вычищаются
полностью (вход — только сотрудникам, жильцы работают через анонимный
QR-портал квартиры), ИИ-пилот (GigaChat, модуль llm) удалён как
бесполезный. Дропаем то, что обслуживало только их:

- device_tokens          — FCM-пуши мобильного приложения (приложение убрано)
- data_refresh_submissions + users.data_refresh_required/_requested_at
                         — popup «актуализируйте данные» в моб. приложении (Bug BB)
- app_releases           — реестр APK-релизов (раздача приложения убрана)
- llm_settings, llm_calls — настройки/журнал GigaChat-пилота
- error_log.ai_analysis/ai_analyzed_at/ai_model — фоновый ИИ-разбор ошибок

Данные жильцов (users/rooms/readings/family/contracts) НЕ трогаем —
это биллинг и сверка 1С. downgrade восстанавливает структуру без данных.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "lk_ai_purge_001"
down_revision = "qr_pass_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS device_tokens")
    op.execute("DROP TABLE IF EXISTS data_refresh_submissions")
    op.execute("DROP TABLE IF EXISTS app_releases")
    op.execute("DROP TABLE IF EXISTS llm_calls")
    op.execute("DROP TABLE IF EXISTS llm_settings")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS data_refresh_required")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS data_refresh_requested_at")
    op.execute("ALTER TABLE error_log DROP COLUMN IF EXISTS ai_analysis")
    op.execute("ALTER TABLE error_log DROP COLUMN IF EXISTS ai_analyzed_at")
    op.execute("ALTER TABLE error_log DROP COLUMN IF EXISTS ai_model")


def downgrade() -> None:
    # Структура без данных — достаточно, чтобы откатить деплой.
    op.add_column("error_log", sa.Column("ai_analysis", JSONB, nullable=True))
    op.add_column("error_log", sa.Column("ai_analyzed_at", sa.DateTime(), nullable=True))
    op.add_column("error_log", sa.Column("ai_model", sa.String(64), nullable=True))
    op.add_column("users", sa.Column(
        "data_refresh_required", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("users", sa.Column("data_refresh_requested_at", sa.DateTime(), nullable=True))
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("token", sa.String(), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "data_refresh_submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("dorm_name", sa.String(), nullable=True),
        sa.Column("room_number", sa.String(), nullable=True),
        sa.Column("residents_count", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.DateTime(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "app_releases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("version_name", sa.String(), nullable=False),
        sa.Column("version_code", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("release_notes", sa.Text(), nullable=True),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("min_required_version_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "llm_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("token_encrypted", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("monthly_budget_tokens", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task", sa.String(), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
