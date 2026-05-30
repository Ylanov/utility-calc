"""alerts_001_resident_problems — таблица сигналов о проблемах жильцов.

Ядро системы сигнализации («Монитор проблем жильца»). Персистентные сигналы
заполняются фоновым сканером scan_resident_problems, читаются колокольчиком /
Inbox / дневным брифингом. Дедупликация по (user_id, problem_type, status):
один OPEN-сигнал на (жилец, тип), повторный скан обновляет last_seen_at,
исчезнувшая проблема авто-закрывается (status=resolved).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = 'alerts_001_resident_problems'
down_revision = 'llm_002_freemium_tokens'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resident_problems",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("problem_type", sa.String(length=50), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False,
                  server_default="medium"),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="open"),
        sa.Column("first_detected_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("snooze_until", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_resident_problems_user_id",
                    "resident_problems", ["user_id"])
    op.create_index("ix_resident_problems_problem_type",
                    "resident_problems", ["problem_type"])
    op.create_index("ix_resident_problems_status",
                    "resident_problems", ["status"])
    op.create_index("ix_resident_problems_user_type_status",
                    "resident_problems", ["user_id", "problem_type", "status"])
    op.create_index("ix_resident_problems_status_severity",
                    "resident_problems", ["status", "severity"])


def downgrade() -> None:
    op.drop_index("ix_resident_problems_status_severity",
                  table_name="resident_problems")
    op.drop_index("ix_resident_problems_user_type_status",
                  table_name="resident_problems")
    op.drop_index("ix_resident_problems_status",
                  table_name="resident_problems")
    op.drop_index("ix_resident_problems_problem_type",
                  table_name="resident_problems")
    op.drop_index("ix_resident_problems_user_id",
                  table_name="resident_problems")
    op.drop_table("resident_problems")
