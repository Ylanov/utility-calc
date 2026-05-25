"""tariff_charge_001 — Tariff.charge_* флаги (Bug AT).

Полное управление «что начисляет этот тариф» — 8 положительных
bool-полей. Default True (zero-impact на существующие тарифы).
Снятая галочка → статья не начисляется никому из жильцов на этом
тарифе. Применимо для сценариев:
  * Лидер — платит всё (default: все 8 = true)
  * Только наём — только charge_social_rent=true, остальные false
  * Без счётчиков — все 4 meter-флага false, остальные true

Отличие от singles_skip_* (Bug AS): те применимы только к жильцам
холостяцкой квартиры (room.is_singles_apartment=true). charge_* —
для всех жильцов на тарифе.
"""
from alembic import op
import sqlalchemy as sa


revision = 'tariff_charge_001'
down_revision = 'singles_apt_001'
branch_labels = None
depends_on = None


_COLS = (
    "charge_hot_water",
    "charge_cold_water",
    "charge_sewage",
    "charge_electricity",
    "charge_maintenance",
    "charge_social_rent",
    "charge_heating",
    "charge_waste",
)


def upgrade() -> None:
    for col in _COLS:
        op.add_column(
            "tariffs",
            sa.Column(
                col,
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )


def downgrade() -> None:
    for col in reversed(_COLS):
        op.drop_column("tariffs", col)
