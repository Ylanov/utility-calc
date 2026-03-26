from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    Boolean,
    Index,
    DateTime,
    func
)
from sqlalchemy.types import Numeric
from sqlalchemy.orm import relationship
from datetime import datetime

from app.core.database import Base
from sqlalchemy.dialects.postgresql import JSONB


# ======================================================
# ROOM (НОВАЯ ОСНОВА СИСТЕМЫ)
# ======================================================
class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)

    dormitory_name = Column(String, index=True)  # общежитие
    room_number = Column(String, index=True)     # комната / квартира

    apartment_area = Column(Numeric(10, 2), default=0.00)
    total_room_residents = Column(Integer, default=1)
    # === НОВЫЕ ПОЛЯ: Номера счетчиков ===
    hw_meter_serial = Column(String, nullable=True)  # ГВС (Горячая вода)
    cw_meter_serial = Column(String, nullable=True)  # ХВС (Холодная вода)
    el_meter_serial = Column(String, nullable=True)  # Электричество

    # Кэш последних показаний (ускорение расчетов)
    last_hot_water = Column(Numeric(12, 3), default=0.000)
    last_cold_water = Column(Numeric(12, 3), default=0.000)
    last_electricity = Column(Numeric(12, 3), default=0.000)

    __table_args__ = (
        Index(
            "uq_room_dormitory_number",
            "dormitory_name",
            "room_number",
            unique=True
        ),
    )


# ======================================================
# USER (ТЕПЕРЬ ТОЛЬКО ЖИЛЕЦ)
# ======================================================
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, nullable=False)

    workplace = Column(String, nullable=True)

    # Сколько человек платит с этого аккаунта
    residents_count = Column(Integer, default=1)

    # 🔑 Связь с комнатой
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=True)
    room = relationship("Room", backref="users")

    totp_secret = Column(String, nullable=True)

    is_deleted = Column(Boolean, default=False, index=True)
    is_initial_setup_done = Column(Boolean, default=False)
    telegram_id = Column(String, unique=True, nullable=True, index=True)

    __table_args__ = (
        # защита от дублей username (без учета регистра)
        Index(
            "uq_user_username_lower",
            func.lower(username),
            unique=True
        ),

        # поиск по username
        Index(
            "idx_user_username_trgm",
            "username",
            postgresql_using="gin",
            postgresql_ops={"username": "gin_trgm_ops"}
        ),
    )


# ======================================================
# TARIFF
# ======================================================
class Tariff(Base):
    __tablename__ = "tariffs"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, default="Базовый тариф")
    is_active = Column(Boolean, default=True, index=True)
    valid_from = Column(DateTime, default=datetime.utcnow)

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
    tariff_id = Column(Integer, ForeignKey("tariffs.id"), nullable=True)
    tariff = relationship("Tariff")

    name = Column(String, unique=True, nullable=False)
    is_active = Column(Boolean, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ======================================================
# ADJUSTMENTS
# ======================================================
class Adjustment(Base):
    __tablename__ = "adjustments"

    id = Column(Integer, primary_key=True, index=True)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    period_id = Column(Integer, ForeignKey("periods.id"), nullable=False)

    amount = Column(Numeric(10, 2), nullable=False)
    description = Column(String, nullable=False)
    account_type = Column(String, default="209", nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    period = relationship("BillingPeriod")

    __table_args__ = (
        Index("idx_adj_user_period", "user_id", "period_id"),
    )


# ======================================================
# METER READING (ПРИВЯЗКА К КОМНАТЕ — КЛЮЧЕВОЕ)
# ======================================================
class MeterReading(Base):
    __tablename__ = "readings"

    __table_args__ = (
        Index("idx_reading_room_period", "room_id", "period_id"),
        Index("idx_reading_approved_period", "is_approved", "period_id"),
        Index("idx_reading_room_approved", "room_id", "is_approved"),
        {
            "postgresql_partition_by": "RANGE (created_at)"
        }
    )

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    created_at = Column(DateTime, primary_key=True, default=datetime.utcnow)

    # 🔑 Главное — комната
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)

    # Кто передал (может быть NULL)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    period_id = Column(Integer, ForeignKey("periods.id"), nullable=True)

    room = relationship("Room")
    user = relationship("User")
    period = relationship("BillingPeriod")

    hot_water = Column(Numeric(12, 3))
    cold_water = Column(Numeric(12, 3))
    electricity = Column(Numeric(12, 3))

    debt_209 = Column(Numeric(12, 2), default=0.00)
    overpayment_209 = Column(Numeric(12, 2), default=0.00)
    debt_205 = Column(Numeric(12, 2), default=0.00)
    overpayment_205 = Column(Numeric(12, 2), default=0.00)

    hot_correction = Column(Numeric(12, 3), default=0.0)
    cold_correction = Column(Numeric(12, 3), default=0.0)
    electricity_correction = Column(Numeric(12, 3), default=0.0)
    sewage_correction = Column(Numeric(12, 3), default=0.0)

    total_209 = Column(Numeric(12, 2), default=0.00)
    total_205 = Column(Numeric(12, 2), default=0.00)
    total_cost = Column(Numeric(12, 2), default=0.00)

    cost_hot_water = Column(Numeric(12, 2), default=0.00)
    cost_cold_water = Column(Numeric(12, 2), default=0.00)
    cost_electricity = Column(Numeric(12, 2), default=0.00)
    cost_sewage = Column(Numeric(12, 2), default=0.00)
    cost_maintenance = Column(Numeric(12, 2), default=0.00)
    cost_social_rent = Column(Numeric(12, 2), default=0.00)
    cost_waste = Column(Numeric(12, 2), default=0.00)
    cost_fixed_part = Column(Numeric(12, 2), default=0.00)

    anomaly_flags = Column(String, nullable=True)
    anomaly_score = Column(Integer, default=0)

    is_approved = Column(Boolean, default=False)

    edit_count = Column(Integer, default=0)
    edit_history = Column(JSONB, nullable=True)


# ======================================================
# SYSTEM SETTINGS
# ======================================================
class SystemSetting(Base):
    __tablename__ = "system_settings"

    key = Column(String, primary_key=True, index=True)
    value = Column(String, nullable=False)
    description = Column(String, nullable=True)


# ======================================================
# DEVICE TOKENS (Для Push-уведомлений)
# ======================================================
class DeviceToken(Base):
    __tablename__ = "device_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    # Сам ключ устройства (FCM Token)
    token = Column(String, unique=True, nullable=False, index=True)

    # Платформа (android, ios, web)
    device_type = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Привязка к пользователю
    user = relationship("User", backref="device_tokens")