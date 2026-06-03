"""readings.room_id → nullable: долг на лицевом счёте (ФИО), не на комнате.

Долг ЖКХ принадлежит лицевому счёту (человеку), а не физической комнате.
Жилец из 1С/ГИС может быть в базе без заселения — его долг всё равно должен
привязываться к НЕМУ (user_id), а комната проставится позже и подцепится.

Снимаем NOT NULL с readings.room_id. Таблица партиц-ная (PARTITION BY RANGE
created_at) — ключ партиций НЕ затрагиваем; DROP NOT NULL на родителе это
изменение метаданных, распространяется на все партиции, без перезаписи.
FK на rooms.id остаётся: NULL внешнему ключу разрешён (нет ссылки).
"""
from alembic import op


revision = 'readings_room_nullable_001'
down_revision = 'users_login_001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE readings ALTER COLUMN room_id DROP NOT NULL")


def downgrade() -> None:
    # Вернуть NOT NULL получится, только если нет строк room_id IS NULL.
    op.execute("ALTER TABLE readings ALTER COLUMN room_id SET NOT NULL")
