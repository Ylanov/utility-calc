"""ot_staff_001: таблица реестра сотрудников по охране труда (КЭС / Основной штат).

Сотрудники — отдельная сущность от жильцов; опц. связь user_id по ФИО.
Структура засевается из Word-шаблонов через POST /api/admin/ot/seed.

Revision ID: ot_staff_001
Revises: readings_room_nullable_001
"""
from alembic import op
import sqlalchemy as sa


revision = "ot_staff_001"
down_revision = "readings_room_nullable_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ot_staff",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=True),
        sa.Column("kes_group", sa.String(length=64), nullable=True),
        sa.Column("department", sa.String(length=255), nullable=True),
        sa.Column("position", sa.String(length=500), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("sout_date", sa.Date(), nullable=True),
        sa.Column("sout_class", sa.String(length=32), nullable=True),
        sa.Column("induction_date", sa.Date(), nullable=True),
        sa.Column("ot_instructions_date", sa.Date(), nullable=True),
        sa.Column("internship_date", sa.Date(), nullable=True),
        sa.Column("siz_note", sa.String(length=255), nullable=True),
        sa.Column("eb_group", sa.String(length=64), nullable=True),
        sa.Column("ot_training_date", sa.Date(), nullable=True),
        sa.Column("ot_training_period_months", sa.Integer(), nullable=True,
                  server_default="36"),
        sa.Column("medical_date", sa.Date(), nullable=True),
        sa.Column("medical_type", sa.String(length=32), nullable=True),
        sa.Column("medical_period_months", sa.Integer(), nullable=True,
                  server_default="12"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ot_staff_id"), "ot_staff", ["id"], unique=False)
    op.create_index(op.f("ix_ot_staff_source"), "ot_staff", ["source"], unique=False)
    op.create_index(op.f("ix_ot_staff_kes_group"), "ot_staff", ["kes_group"], unique=False)
    op.create_index(op.f("ix_ot_staff_department"), "ot_staff", ["department"], unique=False)
    op.create_index(op.f("ix_ot_staff_full_name"), "ot_staff", ["full_name"], unique=False)
    op.create_index(op.f("ix_ot_staff_user_id"), "ot_staff", ["user_id"], unique=False)
    op.create_index(op.f("ix_ot_staff_is_active"), "ot_staff", ["is_active"], unique=False)
    op.create_index("ix_ot_staff_source_kes_dept", "ot_staff",
                    ["source", "kes_group", "department"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ot_staff_source_kes_dept", table_name="ot_staff")
    op.drop_index(op.f("ix_ot_staff_is_active"), table_name="ot_staff")
    op.drop_index(op.f("ix_ot_staff_user_id"), table_name="ot_staff")
    op.drop_index(op.f("ix_ot_staff_full_name"), table_name="ot_staff")
    op.drop_index(op.f("ix_ot_staff_department"), table_name="ot_staff")
    op.drop_index(op.f("ix_ot_staff_kes_group"), table_name="ot_staff")
    op.drop_index(op.f("ix_ot_staff_source"), table_name="ot_staff")
    op.drop_index(op.f("ix_ot_staff_id"), table_name="ot_staff")
    op.drop_table("ot_staff")
