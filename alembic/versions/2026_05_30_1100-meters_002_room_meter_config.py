"""meters_002_room_meter_config — наличие счётчиков на уровне КОМНАТЫ.

Переносит конфигурацию «какие счётчики есть» с жильца (User.has_*_meter,
миграция meters_001) на комнату (Room.has_*_meter). Квартира статична —
настраивается один раз, жилец наследует. Жилец съехал/приехал — конфиг
остаётся на квартире.

Data-migration: room.has_X = false, если в комнате есть жильцы и ВСЕ они
имеют has_X_meter=false (перенос существующих per-user настроек). Иначе
остаётся true (server_default) — zero-impact для не настроенных комнат.
"""
from alembic import op
import sqlalchemy as sa


revision = 'meters_002_room_meter_config'
down_revision = 'alerts_001_resident_problems'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column(
        "has_hw_meter", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("rooms", sa.Column(
        "has_cw_meter", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("rooms", sa.Column(
        "has_el_meter", sa.Boolean(), nullable=False, server_default="true"))

    # Перенос per-user настроек: комната без счётчика X, если в ней есть
    # жильцы и НИ ОДИН не имеет has_X_meter=true.
    for col in ("has_hw_meter", "has_cw_meter", "has_el_meter"):
        op.execute(f"""
            UPDATE rooms r SET {col} = false
            WHERE EXISTS (
                SELECT 1 FROM users u
                WHERE u.room_id = r.id AND u.is_deleted = false
            )
            AND NOT EXISTS (
                SELECT 1 FROM users u
                WHERE u.room_id = r.id AND u.is_deleted = false AND u.{col} = true
            )
        """)


def downgrade() -> None:
    op.drop_column("rooms", "has_el_meter")
    op.drop_column("rooms", "has_cw_meter")
    op.drop_column("rooms", "has_hw_meter")
