from pydantic import BaseModel, condecimal, Field
from typing import Optional, List, Generic, TypeVar
from datetime import datetime
from decimal import Decimal


# ======================================================
# DECIMAL TYPES
# ======================================================

# Для денег (2 знака)
DecimalAmount = condecimal(max_digits=12, decimal_places=2)

# Для объемов (3 знака)
DecimalVolume = condecimal(max_digits=12, decimal_places=3)

# Для тарифов (4 знака)
DecimalTariff = condecimal(max_digits=10, decimal_places=4)


# ======================================================
# PAGINATION
# ======================================================

M = TypeVar("M")


class PaginatedResponse(BaseModel, Generic[M]):
    total: int = Field(..., description="Общее количество записей")
    page: int = Field(..., description="Текущая страница (начиная с 1)")
    size: int = Field(..., description="Количество элементов на странице")
    items: List[M] = Field(..., description="Список элементов")


# ======================================================
# USER SCHEMAS
# ======================================================

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"
    dormitory: str = ""
    workplace: str = ""
    residents_count: int = 1
    total_room_residents: int = 1
    apartment_area: DecimalAmount = Decimal("0.00")


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    dormitory: Optional[str] = None
    workplace: Optional[str] = None
    residents_count: int
    total_room_residents: int
    apartment_area: Optional[Decimal] = None

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    dormitory: Optional[str] = None
    workplace: Optional[str] = None
    residents_count: Optional[int] = None
    total_room_residents: Optional[int] = None
    apartment_area: Optional[DecimalAmount] = None


# ======================================================
# TARIFF SCHEMAS
# ======================================================

class TariffSchema(BaseModel):
    maintenance_repair: DecimalTariff
    social_rent: DecimalTariff
    heating: DecimalTariff
    water_heating: DecimalTariff
    water_supply: DecimalTariff
    sewage: DecimalTariff
    waste_disposal: DecimalTariff
    electricity_per_sqm: DecimalTariff
    electricity_rate: DecimalTariff

    class Config:
        from_attributes = True


# ======================================================
# PERIOD SCHEMAS
# ======================================================

class PeriodCreate(BaseModel):
    name: str


class PeriodResponse(BaseModel):
    id: int
    name: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# ======================================================
# ADJUSTMENT SCHEMAS
# ======================================================

class AdjustmentCreate(BaseModel):
    user_id: int
    amount: DecimalAmount
    description: str


class AdjustmentResponse(BaseModel):
    id: int
    amount: Decimal
    description: str
    created_at: datetime

    class Config:
        from_attributes = True


# ======================================================
# READING SCHEMAS
# ======================================================

class ReadingSchema(BaseModel):
    hot_water: DecimalVolume
    cold_water: DecimalVolume
    electricity: DecimalVolume


class ReadingStateResponse(BaseModel):
    period_name: Optional[str] = None

    prev_hot: Decimal
    prev_cold: Decimal
    prev_elect: Decimal

    current_hot: Optional[Decimal]
    current_cold: Optional[Decimal]
    current_elect: Optional[Decimal]

    total_cost: Optional[Decimal]

    is_draft: bool
    is_period_open: bool

    cost_hot_water: Optional[Decimal] = None
    cost_cold_water: Optional[Decimal] = None
    cost_electricity: Optional[Decimal] = None
    cost_sewage: Optional[Decimal] = None
    cost_maintenance: Optional[Decimal] = None
    cost_social_rent: Optional[Decimal] = None
    cost_waste: Optional[Decimal] = None
    cost_fixed_part: Optional[Decimal] = None


class ApproveRequest(BaseModel):
    hot_correction: DecimalVolume
    cold_correction: DecimalVolume
    electricity_correction: DecimalVolume
    sewage_correction: DecimalVolume


# ======================================================
# FINANCIER RESPONSE (UPDATED)
# ======================================================

class UserDebtResponse(BaseModel):
    id: int
    username: str
    dormitory: Optional[str] = None

    # Отдаем Decimal без преобразования во float
    initial_debt: Decimal
    initial_overpayment: Decimal
    current_total_cost: Decimal

    class Config:
        from_attributes = True
