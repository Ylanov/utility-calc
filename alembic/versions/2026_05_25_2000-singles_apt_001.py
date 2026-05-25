"""singles_apt_001 — холостяцкие квартиры (Bug AS).

Добавляет на уровень Room и Tariff поля для бизнес-логики
«холостяцких квартир»:

  Room:
    - is_singles_apartment (bool, default False) — вся квартира
      холостяцкая. Все жильцы внутри получают равные доли счёта.
    - max_capacity (int, nullable) — макс. вместимость квартиры
      (2-шка, 3-шка и т.д.). Информационное поле.

  Tariff:
    - singles_skip_maintenance (bool, default False)
    - singles_skip_social_rent (bool, default False)
    - singles_skip_heating (bool, default False)
    - singles_skip_waste (bool, default False)
    Все 4 — «не начислять для холостяцких квартир». Default False
    сохраняет поведение для существующих тарифов.

Никаких изменений в данных — только структура. is_singles_apartment
выставляется админом через UI «Жилфонд» после деплоя.
"""
from alembic import op
import sqlalchemy as sa


revision = 'singles_apt_001'
down_revision = 'residency_001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Room
    op.add_column(
        "rooms",
        sa.Column(
            "is_singles_apartment",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "idx_rooms_is_singles_apartment", "rooms", ["is_singles_apartment"]
    )
    op.add_column(
        "rooms",
        sa.Column("max_capacity", sa.Integer(), nullable=True),
    )

    # Tariff — 4 skip-флага для холостяцких квартир
    for col in (
        "singles_skip_maintenance",
        "singles_skip_social_rent",
        "singles_skip_heating",
        "singles_skip_waste",
    ):
        op.add_column(
            "tariffs",
            sa.Column(
                col,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    for col in (
        "singles_skip_waste",
        "singles_skip_heating",
        "singles_skip_social_rent",
        "singles_skip_maintenance",
    ):
        op.drop_column("tariffs", col)
    op.drop_column("rooms", "max_capacity")
    op.drop_index("idx_rooms_is_singles_apartment", table_name="rooms")
    op.drop_column("rooms", "is_singles_apartment")
