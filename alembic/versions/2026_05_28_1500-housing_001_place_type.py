"""housing_001_place_type — Room.place_type + структурированный адрес дома.

Этап 1 рефакторинга Жилфонда: разделяем помещения на два типа:

  Room.place_type = 'dormitory'  (старое поведение, default)
    Адрес: dormitory_name + room_number.
    Жильцы могут быть family или per_capita (койко-место).
    Подают показания счётчиков (hw/cw/el).
    Тариф рассчитывает все статьи.

  Room.place_type = 'house'      (новое)
    Адрес: street + house_number + apartment_number.
    Жильцы — только семейные (per_capita запрещён).
    Счётчики не нужны, показания не подаются.
    Тариф — начисление ТОЛЬКО найма (через charge_*-флаги, уже существуют).

Tariff.applicable_to = 'dormitory' | 'house' | 'both' (default 'both')
  Позволяет UI фильтровать список тарифов в зависимости от типа Room.
  На начислении не отражается — это только удобство в админке.

ИЗМЕНЕНИЯ:
  1. Room: добавить колонки place_type, street, house_number, apartment_number.
  2. Tariff: добавить колонку applicable_to.
  3. Снять старый unique index uq_room_dormitory_number, заменить на ДВА
     partial unique:
       - uq_room_dorm_addr WHERE place_type='dormitory' — (dormitory_name, room_number).
       - uq_room_house_addr WHERE place_type='house' — (street, house_number, apartment_number).
  4. CHECK constraint: для каждого place_type требуем соответствующие поля
     адреса заполненными (защита от мусора в БД мимо UI).

Никаких изменений данных — все существующие комнаты получают
place_type='dormitory' через server_default и продолжают работать как
раньше. Новые типы создаются админом через UI после деплоя.
"""
from alembic import op
import sqlalchemy as sa


revision = 'housing_001_place_type'
down_revision = 'data_refresh_001'
branch_labels = None
depends_on = None


# ENUM-типы создаём как Postgres native enum через CREATE TYPE — это даёт
# валидацию на уровне БД (любой INSERT с place_type='garage' упадёт сразу,
# а не где-то в Python). Имена типов с префиксом для удобства DDL-аудита.
_PLACE_TYPE_NAME = "place_type_enum"
_APPLICABLE_TO_NAME = "tariff_applicable_to_enum"


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────
    # 1. CREATE TYPE place_type_enum + добавить колонку.
    # ─────────────────────────────────────────────────────────────────
    op.execute(f"CREATE TYPE {_PLACE_TYPE_NAME} AS ENUM ('dormitory', 'house')")
    op.add_column(
        "rooms",
        sa.Column(
            "place_type",
            sa.Enum("dormitory", "house", name=_PLACE_TYPE_NAME, create_type=False),
            nullable=False,
            server_default=sa.text("'dormitory'::" + _PLACE_TYPE_NAME),
        ),
    )
    op.create_index("idx_rooms_place_type", "rooms", ["place_type"])

    # ─────────────────────────────────────────────────────────────────
    # 2. Адресные колонки для домов/квартир (все nullable — обязательность
    # обеспечивается CHECK-constraint'ом ниже, в зависимости от place_type).
    # ─────────────────────────────────────────────────────────────────
    op.add_column("rooms", sa.Column("street", sa.String(length=200), nullable=True))
    op.add_column("rooms", sa.Column("house_number", sa.String(length=50), nullable=True))
    op.add_column("rooms", sa.Column("apartment_number", sa.String(length=50), nullable=True))

    # ─────────────────────────────────────────────────────────────────
    # 3. CHECK-constraint: либо «общага-адрес», либо «дом-адрес» —
    # в зависимости от place_type. Заполняет защиту на уровне БД от
    # некорректных комбинаций (например, place_type='house', но street
    # пустой). UI повторяет ту же логику для приветливых сообщений.
    # ─────────────────────────────────────────────────────────────────
    op.create_check_constraint(
        "ck_rooms_address_matches_place_type",
        "rooms",
        (
            # Для общежития: dormitory_name И room_number должны быть заполнены.
            "(place_type = 'dormitory' AND dormitory_name IS NOT NULL "
            "AND dormitory_name <> '' "
            "AND room_number IS NOT NULL AND room_number <> '') "
            "OR "
            # Для дома: street, house_number и apartment_number обязательны.
            "(place_type = 'house' AND street IS NOT NULL AND street <> '' "
            "AND house_number IS NOT NULL AND house_number <> '' "
            "AND apartment_number IS NOT NULL AND apartment_number <> '')"
        ),
    )

    # ─────────────────────────────────────────────────────────────────
    # 4. CREATE TYPE tariff_applicable_to_enum + Tariff.applicable_to.
    # 'both' — обратная совместимость: существующие тарифы видны и для
    # общаг, и для домов до тех пор пока админ явно не пометит их одним
    # из конкретных типов.
    # ─────────────────────────────────────────────────────────────────
    op.execute(
        f"CREATE TYPE {_APPLICABLE_TO_NAME} AS ENUM ('dormitory', 'house', 'both')"
    )
    op.add_column(
        "tariffs",
        sa.Column(
            "applicable_to",
            sa.Enum(
                "dormitory", "house", "both",
                name=_APPLICABLE_TO_NAME, create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'both'::" + _APPLICABLE_TO_NAME),
        ),
    )
    op.create_index("idx_tariffs_applicable_to", "tariffs", ["applicable_to"])

    # ─────────────────────────────────────────────────────────────────
    # 5. Снимаем старый unique-индекс (dormitory_name, room_number) —
    # он не учитывает place_type, поэтому второй partial-index для домов
    # не может с ним сосуществовать. Заменяем на два partial:
    #   - uq_room_dorm_addr  WHERE place_type='dormitory'
    #   - uq_room_house_addr WHERE place_type='house'
    # ─────────────────────────────────────────────────────────────────
    op.drop_index("uq_room_dormitory_number", table_name="rooms")
    op.execute(
        "CREATE UNIQUE INDEX uq_room_dorm_addr "
        "ON rooms (dormitory_name, room_number) "
        "WHERE place_type = 'dormitory'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_room_house_addr "
        "ON rooms (street, house_number, apartment_number) "
        "WHERE place_type = 'house'"
    )

    # GIN-индекс на street для быстрого ILIKE-поиска по улице (аналог
    # idx_user_dormitory_trgm для users.dormitory). Без него /api/rooms
    # с search по адресу будет сканировать всю таблицу. Расширение
    # pg_trgm уже включено более ранней миграцией b6fada547a41.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rooms_street_trgm ON rooms "
        "USING gin (street gin_trgm_ops) WHERE street IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_rooms_street_trgm")
    op.execute("DROP INDEX IF EXISTS uq_room_house_addr")
    op.execute("DROP INDEX IF EXISTS uq_room_dorm_addr")
    # Восстанавливаем старый unique-индекс. ВАЖНО: если в проде уже есть
    # дома (place_type='house'), их dormitory_name/room_number = NULL —
    # старый unique не упадёт на нескольких NULL (Postgres трактует
    # NULL как разные значения), но в логике приложения такие строки
    # будут невалидными. downgrade на проде с домами не делаем —
    # downgrade существует только для откатной возможности в dev/CI.
    op.create_index(
        "uq_room_dormitory_number", "rooms",
        ["dormitory_name", "room_number"], unique=True,
    )

    op.drop_index("idx_tariffs_applicable_to", table_name="tariffs")
    op.drop_column("tariffs", "applicable_to")
    op.execute(f"DROP TYPE IF EXISTS {_APPLICABLE_TO_NAME}")

    op.drop_constraint(
        "ck_rooms_address_matches_place_type", "rooms", type_="check",
    )
    op.drop_column("rooms", "apartment_number")
    op.drop_column("rooms", "house_number")
    op.drop_column("rooms", "street")

    op.drop_index("idx_rooms_place_type", table_name="rooms")
    op.drop_column("rooms", "place_type")
    op.execute(f"DROP TYPE IF EXISTS {_PLACE_TYPE_NAME}")
