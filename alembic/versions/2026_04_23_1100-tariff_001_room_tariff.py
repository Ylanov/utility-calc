"""Room.tariff_id — тариф можно привязать к помещению, не только к жильцу

Revision ID: tariff_001_room_tariff
Revises: analyzer_001_settings
Create Date: 2026-04-23 11:00:00.000000

Сценарий: «парочка тарифов для разных мест жительства» — пользователь хочет
у общежития № 5 один тариф, у № 7 другой. До сих пор тариф был только в User
(`User.tariff_id`), и для смены тарифа для всего общежития надо было пройти
по всем жильцам. Теперь:

   приоритет матча = Room.tariff_id → User.tariff_id → default tariff (id=1)

Логика приоритета — в новом сервисе `tariff_cache.get_effective_tariff()`.
Эта миграция только добавляет колонку и индекс.
"""
from alembic import op
import sqlalchemy as sa


revision = 'tariff_001_room_tariff'
down_revision = 'analyzer_001_settings'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'rooms',
        sa.Column('tariff_id', sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        'fk_rooms_tariff_id',
        'rooms', 'tariffs',
        ['tariff_id'], ['id'],
        ondelete='SET NULL',  # удаление тарифа не должно удалять комнату
    )
    op.create_index(
        'ix_rooms_tariff_id',
        'rooms', ['tariff_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_rooms_tariff_id', table_name='rooms')
    op.drop_constraint('fk_rooms_tariff_id', 'rooms', type_='foreignkey')
    op.drop_column('rooms', 'tariff_id')
