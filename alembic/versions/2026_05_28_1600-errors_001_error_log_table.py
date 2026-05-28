"""errors_001_error_log_table — таблица для копилки ошибок системы.

E3-A (28.05.2026): унифицированная копилка всех ошибок платформы:
- backend 500 (unhandled exceptions через middleware);
- backend 4xx (HTTPException 400/422/409 — по флагу `errors.capture_4xx`);
- celery worker failures (через task_failure signal);
- frontend JS-ошибки (window.onerror / unhandledrejection → POST endpoint).

Запись содержит достаточно контекста для самостоятельной диагностики:
- traceback, request body, user, request_id;
- `investigation` JSONB — автоматически собранный контекст по URL
  (gsheets-row + связанный жилец + room + recent readings + recent audit);
- `extra` JSONB — произвольные доп. поля.

Используется через `app.core.error_logger.log_error()` и
`app.core.middleware.error_capture.ErrorCaptureMiddleware`.

Админ видит ошибки в /api/admin/errors с кнопкой «Скопировать в Claude» —
markdown-дамп для вставки в чат с AI-ассистентом.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = 'errors_001_error_log'
down_revision = 'housing_001_place_type'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "error_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "occurred_at", sa.DateTime(),
            nullable=False, server_default=sa.text("(now() AT TIME ZONE 'utc')"),
            index=True,
        ),
        # backend / celery / frontend
        sa.Column("source", sa.String(length=20), nullable=False, index=True),
        # error / warning / info
        sa.Column(
            "level", sa.String(length=10),
            nullable=False, server_default="error", index=True,
        ),

        # HTTP-контекст (только для backend / frontend)
        sa.Column("http_method", sa.String(length=10), nullable=True),
        sa.Column("http_path", sa.String(length=500), nullable=True, index=True),
        sa.Column("http_status", sa.Integer(), nullable=True, index=True),

        # Исключение
        sa.Column("exc_type", sa.String(length=200), nullable=True, index=True),
        sa.Column("exc_message", sa.Text(), nullable=True),
        sa.Column("traceback", sa.Text(), nullable=True),

        # Тело запроса (только полезные fields, без секретов)
        sa.Column("request_body", JSONB, nullable=True),

        # Кто
        sa.Column("user_id", sa.Integer(), nullable=True, index=True),
        sa.Column("user_username", sa.String(length=200), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True, index=True),

        # Авто-расследование: связанные сущности
        sa.Column("investigation", JSONB, nullable=True),
        # Произвольные доп. поля (celery task name, args; JS file/line)
        sa.Column("extra", JSONB, nullable=True),

        # Lifecycle админа
        sa.Column(
            "resolved", sa.Boolean(),
            nullable=False, server_default=sa.text("false"), index=True,
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by_id", sa.Integer(), nullable=True),
        sa.Column("resolved_notes", sa.Text(), nullable=True),

        # Сколько раз эту запись копировали — метрика «насколько полезно админу»
        sa.Column(
            "copied_count", sa.Integer(),
            nullable=False, server_default="0",
        ),
    )

    # Композитный индекс для типичного запроса «свежие unresolved».
    op.create_index(
        "idx_error_log_recent_unresolved",
        "error_log",
        ["resolved", "occurred_at"],
        postgresql_where=sa.text("resolved = false"),
    )


def downgrade() -> None:
    op.drop_index("idx_error_log_recent_unresolved", table_name="error_log")
    op.drop_table("error_log")
