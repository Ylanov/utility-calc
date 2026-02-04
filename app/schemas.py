
from pydantic import BaseModel
from typing import Optional
from datetime import datetime


# --- USER ---
class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"
    dormitory: str = ""
    workplace: str = ""
    residents_count: int = 1
    total_room_residents: int = 1
    apartment_area: float = 0.0


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    dormitory: Optional[str]
    workplace: Optional[str]
    residents_count: int
    total_room_residents: int
    apartment_area: float

    class Config:
        from_attributes = True


# <<< НОВАЯ СХЕМА ДЛЯ ОБНОВЛЕНИЯ >>>
class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    dormitory: Optional[str] = None
    workplace: Optional[str] = None
    residents_count: Optional[int] = None
    total_room_residents: Optional[int] = None
    apartment_area: Optional[float] = None


# --- TARIFF ---
class TariffSchema(BaseModel):
    maintenance_repair: float
    social_rent: float
    heating: float
    water_heating: float
    water_supply: float
    sewage: float
    waste_disposal: float
    electricity_per_sqm: float
    electricity_rate: float

    class Config:
        from_attributes = True


# --- PERIODS (НОВОЕ) ---
class PeriodCreate(BaseModel):
    name: str  # "Февраль 2025"


class PeriodResponse(BaseModel):
    id: int
    name: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# --- READING ---
class ReadingSchema(BaseModel):
    hot_water: float
    cold_water: float
    electricity: float


class ReadingStateResponse(BaseModel):
    # Добавляем информацию о периоде
    period_name: Optional[str] = None

    prev_hot: float
    prev_cold: float
    prev_elect: float
    current_hot: Optional[float]
    current_cold: Optional[float]
    current_elect: Optional[float]
    total_cost: Optional[float]
    is_draft: bool

    cost_hot_water: Optional[float] = None
    cost_cold_water: Optional[float] = None
    cost_electricity: Optional[float] = None
    cost_sewage: Optional[float] = None
    cost_maintenance: Optional[float] = None
    cost_social_rent: Optional[float] = None
    cost_waste: Optional[float] = None
    cost_fixed_part: Optional[float] = None


class ApproveRequest(BaseModel):
    hot_correction: float
    cold_correction: float
    electricity_correction: float
    sewage_correction: float
