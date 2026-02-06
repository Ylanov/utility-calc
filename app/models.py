from sqlalchemy import Column, Integer, String, ForeignKey, Boolean, Index, DateTime
from sqlalchemy.types import Numeric  # <--- ЗАМЕНИЛИ Float на Numeric
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base


# --- USER ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    role = Column(String)
    dormitory = Column(String, nullable=True, index=True)
    workplace = Column(String, nullable=True)
    residents_count = Column(Integer, default=1)
    total_room_residents = Column(Integer, default=1)
    # Площадь важна для расчетов, берем 2 знака
    apartment_area = Column(Numeric(10, 2), default=0.00)


# --- TARIFF ---
class Tariff(Base):
    __tablename__ = "tariffs"
    id = Column(Integer, primary_key=True)
    # Тарифы с запасом точности (4 знака)
    maintenance_repair = Column(Numeric(10, 4), default=0.0)
    social_rent = Column(Numeric(10, 4), default=0.0)
    heating = Column(Numeric(10, 4), default=0.0)
    water_heating = Column(Numeric(10, 4), default=0.0)
    water_supply = Column(Numeric(10, 4), default=0.0)
    sewage = Column(Numeric(10, 4), default=0.0)
    waste_disposal = Column(Numeric(10, 4), default=0.0)
    electricity_per_sqm = Column(Numeric(10, 4), default=0.0)
    electricity_rate = Column(Numeric(10, 4), default=5.0)


# --- PERIODS ---
class BillingPeriod(Base):
    __tablename__ = "periods"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# --- METER READING ---
class MeterReading(Base):
    __tablename__ = "readings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    period_id = Column(Integer, ForeignKey("periods.id"), nullable=True)

    user = relationship("User")
    period = relationship("BillingPeriod")

    # Объемы (3 знака после запятой)
    hot_water = Column(Numeric(12, 3))
    cold_water = Column(Numeric(12, 3))
    electricity = Column(Numeric(12, 3))

    # Коррекции
    hot_correction = Column(Numeric(12, 3), default=0.0)
    cold_correction = Column(Numeric(12, 3), default=0.0)
    electricity_correction = Column(Numeric(12, 3), default=0.0)
    sewage_correction = Column(Numeric(12, 3), default=0.0)

    # Деньги (Строго 2 знака)
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
    is_approved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index('idx_user_period', 'user_id', 'period_id'),
        Index('idx_approved_period', 'is_approved', 'period_id'),
    )