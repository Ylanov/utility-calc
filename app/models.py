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

    # ФИО / логин
    username = Column(String, unique=True, index=True)

    hashed_password = Column(String, nullable=False)

    role = Column(String, nullable=False)

    dormitory = Column(String, nullable=True, index=True)

    workplace = Column(String, nullable=True)

    residents_count = Column(Integer, default=1)

    total_room_residents = Column(Integer, default=1)

    # Площадь (2 знака после запятой)
    apartment_area = Column(Numeric(10, 2), default=0.00)

    # Функциональный индекс для быстрого поиска без учета регистра
    __table_args__ = (
        Index("idx_user_username_lower", func.lower(username)),
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

    created_at = Column(DateTime, default=datetime.utcnow)

    # Связи
    user = relationship("User")
    period = relationship("BillingPeriod")

    # Индекс для быстрого поиска корректировок пользователя в конкретном периоде
    __table_args__ = (
        Index("idx_adj_user_period", "user_id", "period_id"),
    )


# ======================================================
# METER READING
# ======================================================
class MeterReading(Base):
    __tablename__ = "readings"

    id = Column(Integer, primary_key=True, index=True)

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
    # САЛЬДО ИЗ 1С
    # ==================================================
    initial_debt = Column(Numeric(12, 2), default=0.00)

    initial_overpayment = Column(Numeric(12, 2), default=0.00)

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
    total_cost = Column(Numeric(12, 2), default=0.00)

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

    created_at = Column(DateTime, default=datetime.utcnow)

    # ==================================================
    # ИНДЕКСЫ
    # ==================================================
    __table_args__ = (
        # Быстрый поиск всех показаний пользователя в периоде
        Index("idx_reading_user_period", "user_id", "period_id"),

        # Быстрый поиск всех утвержденных/черновиков в периоде (для bulk операций)
        Index("idx_reading_approved_period", "is_approved", "period_id"),

        # Быстрый поиск истории утвержденных показаний пользователя (для сверки)
        Index("idx_reading_user_approved", "user_id", "is_approved"),
    )