from sqlalchemy import Column, Integer, Float, DateTime, String, ForeignKey, Boolean, Index
from sqlalchemy.orm import relationship
from datetime import datetime
from app.database import Base

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True) # Index ускоряет вход
    hashed_password = Column(String)
    role = Column(String)
    dormitory = Column(String, nullable=True, index=True) # Index ускоряет фильтр по общаге
    workplace = Column(String, nullable=True)
    residents_count = Column(Integer, default=1)
    total_room_residents = Column(Integer, default=1)
    apartment_area = Column(Float, default=0.0)

class Tariff(Base):
    __tablename__ = "tariffs"
    id = Column(Integer, primary_key=True)
    maintenance_repair = Column(Float, default=0.0)
    social_rent = Column(Float, default=0.0)
    heating = Column(Float, default=0.0)
    water_heating = Column(Float, default=0.0)
    water_supply = Column(Float, default=0.0)
    sewage = Column(Float, default=0.0)
    waste_disposal = Column(Float, default=0.0)
    electricity_per_sqm = Column(Float, default=0.0)
    electricity_rate = Column(Float, default=5.0)

# НОВАЯ ТАБЛИЦА: Периоды (Месяцы)
class BillingPeriod(Base):
    __tablename__ = "periods"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True) # Например "Январь 2025"
    is_active = Column(Boolean, default=True) # Активен сейчас?
    created_at = Column(DateTime, default=datetime.utcnow)

class MeterReading(Base):
    __tablename__ = "readings"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    period_id = Column(Integer, ForeignKey("periods.id"), nullable=True)

    # ДОБАВИТЬ ЭТИ ДВЕ СТРОКИ
    user = relationship("User")
    period = relationship("BillingPeriod")

    hot_water = Column(Float)
    cold_water = Column(Float)
    electricity = Column(Float)

    hot_correction = Column(Float, default=0.0)
    cold_correction = Column(Float, default=0.0)
    electricity_correction = Column(Float, default=0.0)
    sewage_correction = Column(Float, default=0.0)

    total_cost = Column(Float, default=0.0)

    cost_hot_water = Column(Float, default=0.0)
    cost_cold_water = Column(Float, default=0.0)
    cost_electricity = Column(Float, default=0.0)
    cost_sewage = Column(Float, default=0.0)
    cost_maintenance = Column(Float, default=0.0)
    cost_social_rent = Column(Float, default=0.0)
    cost_waste = Column(Float, default=0.0)
    cost_fixed_part = Column(Float, default=0.0)
    anomaly_flags = Column(String, nullable=True)  # Храним теги через запятую, например "HIGH_HOT,ZERO_COLD"

    is_approved = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # ИНДЕКСЫ ДЛЯ ПРОИЗВОДИТЕЛЬНОСТИ
    # Быстрый поиск показаний конкретного юзера
    # Быстрый поиск по статусу утверждения (для админки)
    # Быстрый поиск по периоду
    __table_args__ = (
        Index('idx_user_period', 'user_id', 'period_id'),
        Index('idx_approved_period', 'is_approved', 'period_id'),
    )