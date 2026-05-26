"""data_refresh_001 — User.data_refresh_required + DataRefreshSubmission (Bug BB).

Админ через админ-панель может запросить у жильца актуальные данные
(общежитие, комната, кол-во проживающих). Жилец видит popup в
мобильном приложении один раз, заполняет, отправляет → submission
сохраняется в `data_refresh_submissions`, флаг снимается.

Поля User:
  - data_refresh_required (bool, default False) — флаг «надо собрать»
  - data_refresh_requested_at (datetime, nullable) — когда админ запросил

Таблица data_refresh_submissions:
  - id, user_id, requested_at (откуда пошёл запрос),
    dorm_name (свободная строка — жилец мог переехать),
    room_number (свободная строка),
    residents_count (int),
    submitted_at (datetime, default now)

ВАЖНО: submission'ы НЕ применяются автоматически к User/Room. Это
log для админа — он сравнивает с системными данными и при расхождении
правит вручную. Защита от опечаток и нечестных ответов.
"""
from alembic import op
import sqlalchemy as sa


revision = 'data_refresh_001'
down_revision = 'tariff_charge_001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Колонки User.
    op.add_column(
        "users",
        sa.Column(
            "data_refresh_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "data_refresh_requested_at",
            sa.DateTime(),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_user_data_refresh_required",
        "users",
        ["data_refresh_required"],
    )

    # 2. Таблица submissions (log).
    op.create_table(
        "data_refresh_submissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requested_at", sa.DateTime(), nullable=True),
        sa.Column("dorm_name", sa.String(length=200), nullable=False),
        sa.Column("room_number", sa.String(length=50), nullable=False),
        sa.Column("residents_count", sa.Integer(), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now() AT TIME ZONE 'utc'"),
        ),
    )
    op.create_index(
        "idx_data_refresh_user",
        "data_refresh_submissions",
        ["user_id"],
    )
    op.create_index(
        "idx_data_refresh_submitted",
        "data_refresh_submissions",
        ["submitted_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_data_refresh_submitted", table_name="data_refresh_submissions")
    op.drop_index("idx_data_refresh_user", table_name="data_refresh_submissions")
    op.drop_table("data_refresh_submissions")
    op.drop_index("idx_user_data_refresh_required", table_name="users")
    op.drop_column("users", "data_refresh_requested_at")
    op.drop_column("users", "data_refresh_required")
