from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    Boolean,
    Index,
    DateTime,
    func,
)
from sqlalchemy.types import Numeric
from sqlalchemy.orm import relationship
from datetime import datetime

from app.database import Base


# ======================================================
# USER
# ======================================================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False)
    dormitory = Column(String, nullable=True, index=True)
    workplace = Column(String, nullable=True)
    residents_count = Column(Integer, default=1)
    total_room_residents = Column(Integer, default=1)
    apartment_area = Column(Numeric(10, 2), default=0.00)

    # Секретный ключ для 2FA (TOTP). Если NULL — 2FA выключена.
    totp_secret = Column(String, nullable=True)

    # Поля для Soft Delete и Первичной настройки
    is_deleted = Column(Boolean, default=False, index=True)
    is_initial_setup_done = Column(Boolean, default=False)

    __table_args__ = (
        # Быстрые GIN индексы для поиска по тексту (pg_trgm)
        Index(
            "idx_user_username_trgm",
            "username",
            postgresql_using="gin",
            postgresql_ops={"username": "gin_trgm_ops"}
        ),
        Index(
            "idx_user_dormitory_trgm",
            "dormitory",
            postgresql_using="gin",
            postgresql_ops={"dormitory": "gin_trgm_ops"}
        ),
    )


# ======================================================
# TARIFF
# ======================================================
class Tariff(Base):
    __tablename__ = "tariffs"

    id = Column(Integer, primary_key=True)

    # Флаг активности тарифа (для версионности и истории)
    is_active = Column(Boolean, default=True, index=True)

    # Дата начала действия тарифа
    valid_from = Column(DateTime, default=datetime.utcnow)

    # Тарифы (4 знака после запятой для точности)
    maintenance_repair = Column(Numeric(10, 4), default=0.0)
    social_rent = Column(Numeric(10, 4), default=0.0)
    heating = Column(Numeric(10, 4), default=0.0)
    water_heating = Column(Numeric(10, 4), default=0.0)
    water_supply = Column(Numeric(10, 4), default=0.0)
    sewage = Column(Numeric(10, 4), default=0.0)
    waste_disposal = Column(Numeric(10, 4), default=0.0)
    electricity_per_sqm = Column(Numeric(10, 4), default=0.0)
    electricity_rate = Column(Numeric(10, 4), default=5.0)


# ======================================================
# BILLING PERIOD
# ======================================================
class BillingPeriod(Base):
    __tablename__ = "periods"

    id = Column(Integer, primary_key=True, index=True)

    name = Column(String, unique=True, nullable=False)

    # Индексируем, так как часто ищем именно активный период
    is_active = Column(Boolean, default=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)


# ======================================================
# ADJUSTMENTS (Финансовые корректировки)
# ======================================================
class Adjustment(Base):
    __tablename__ = "adjustments"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    period_id = Column(Integer, ForeignKey("periods.id"), nullable=False)

    # Сумма корректировки (может быть отрицательной)
    amount = Column(Numeric(10, 2), nullable=False)

    description = Column(String, nullable=False)

    # Тип счета ('209' - коммуналка, '205' - найм)
    account_type = Column(String, default="209", nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Связи
    user = relationship("User")
    period = relationship("BillingPeriod")

    # Индекс для быстрого поиска корректировок пользователя в конкретном периоде
    __table_args__ = (
        Index("idx_adj_user_period", "user_id", "period_id"),
    )


# ======================================================
# METER READING (Партицированная таблица)
# ======================================================
class MeterReading(Base):
    __tablename__ = "readings"

    # ВАЖНО: Настройка партицирования PostgreSQL
    __table_args__ = (
        Index("idx_reading_user_period", "user_id", "period_id"),
        Index("idx_reading_approved_period", "is_approved", "period_id"),
        Index("idx_reading_user_approved", "user_id", "is_approved"),
        {
            "postgresql_partition_by": "RANGE (created_at)"
        }
    )

    # В партицированных таблицах ключ партицирования (created_at) ОБЯЗАН входить в Primary Key
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at = Column(DateTime, primary_key=True, default=datetime.utcnow)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    period_id = Column(Integer, ForeignKey("periods.id"), nullable=True)

    # Связи
    user = relationship("User")
    period = relationship("BillingPeriod")

    # ==================================================
    # ОБЪЕМЫ (3 знака)
    # ==================================================
    hot_water = Column(Numeric(12, 3))
    cold_water = Column(Numeric(12, 3))
    electricity = Column(Numeric(12, 3))

    # ==================================================
    # САЛЬДО ИЗ 1С (РАЗДЕЛЬНЫЙ УЧЕТ)
    # ==================================================
    debt_209 = Column(Numeric(12, 2), default=0.00)
    overpayment_209 = Column(Numeric(12, 2), default=0.00)

    debt_205 = Column(Numeric(12, 2), default=0.00)
    overpayment_205 = Column(Numeric(12, 2), default=0.00)

    # ==================================================
    # КОРРЕКЦИИ ОБЪЕМОВ
    # ==================================================
    hot_correction = Column(Numeric(12, 3), default=0.0)
    cold_correction = Column(Numeric(12, 3), default=0.0)
    electricity_correction = Column(Numeric(12, 3), default=0.0)
    sewage_correction = Column(Numeric(12, 3), default=0.0)

    # ==================================================
    # ДЕНЕЖНЫЕ РАСЧЕТЫ (2 знака)
    # ==================================================
    total_209 = Column(Numeric(12, 2), default=0.00)
    total_205 = Column(Numeric(12, 2), default=0.00)
    total_cost = Column(Numeric(12, 2), default=0.00)

    # Детализация
    cost_hot_water = Column(Numeric(12, 2), default=0.00)
    cost_cold_water = Column(Numeric(12, 2), default=0.00)
    cost_electricity = Column(Numeric(12, 2), default=0.00)
    cost_sewage = Column(Numeric(12, 2), default=0.00)
    cost_maintenance = Column(Numeric(12, 2), default=0.00)
    cost_social_rent = Column(Numeric(12, 2), default=0.00)
    cost_waste = Column(Numeric(12, 2), default=0.00)
    cost_fixed_part = Column(Numeric(12, 2), default=0.00)

    # ==================================================
    # СЛУЖЕБНЫЕ ПОЛЯ
    # ==================================================
    anomaly_flags = Column(String, nullable=True)
    is_approved = Column(Boolean, default=False)