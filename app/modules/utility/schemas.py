# app/modules/utility/schemas.py

from pydantic import BaseModel, condecimal, Field, ConfigDict
from typing import Optional, List, Generic, TypeVar, Literal
from datetime import datetime
from decimal import Decimal

# ======================================================
# DECIMAL TYPES
# ======================================================

DecimalAmount = condecimal(max_digits=12, decimal_places=2)
DecimalVolume = condecimal(max_digits=12, decimal_places=3)
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
# ROOM SCHEMAS
# ======================================================

class RoomCreate(BaseModel):
    dormitory_name: str
    room_number: str
    apartment_area: DecimalAmount
    total_room_residents: int = 1
    hw_meter_serial: Optional[str] = None
    cw_meter_serial: Optional[str] = None
    el_meter_serial: Optional[str] = None


class RoomUpdate(BaseModel):
    dormitory_name: Optional[str] = None
    room_number: Optional[str] = None
    apartment_area: Optional[DecimalAmount] = None
    total_room_residents: Optional[int] = None
    hw_meter_serial: Optional[str] = None
    cw_meter_serial: Optional[str] = None
    el_meter_serial: Optional[str] = None


class RoomResponse(BaseModel):
    id: int
    dormitory_name: str
    room_number: str
    apartment_area: DecimalAmount
    total_room_residents: int
    hw_meter_serial: Optional[str] = None
    cw_meter_serial: Optional[str] = None
    el_meter_serial: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ======================================================
# USER SCHEMAS
# ======================================================

AllowedRole = Literal["user", "accountant", "financier", "admin"]
AllowedAccountType = Literal["209", "205"]


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=8, max_length=128)
    role: AllowedRole = "user"
    workplace: str = ""
    residents_count: int = Field(1, ge=1, le=20)
    tariff_id: Optional[int] = None
    room_id: Optional[int] = None
    hw_meter_serial: Optional[str] = None
    cw_meter_serial: Optional[str] = None
    el_meter_serial: Optional[str] = None


class UserResponse(BaseModel):
    id: int
    username: str
    role: str

    workplace: Optional[str] = None
    residents_count: int

    tariff_id: Optional[int] = None

    is_2fa_enabled: bool = False
    is_initial_setup_done: bool = False

    room: Optional[RoomResponse] = None

    model_config = ConfigDict(from_attributes=True)


class UserUpdate(BaseModel):
    username: Optional[str] = Field(None, min_length=3, max_length=100)
    password: Optional[str] = Field(None, min_length=8, max_length=128)
    role: Optional[AllowedRole] = None
    workplace: Optional[str] = None
    residents_count: Optional[int] = Field(None, ge=1, le=20)
    tariff_id: Optional[int] = None
    room_id: Optional[int] = None
    hw_meter_serial: Optional[str] = None
    cw_meter_serial: Optional[str] = None
    el_meter_serial: Optional[str] = None


# ======================================================
# 2FA (TOTP) SCHEMAS
# ======================================================

class TotpSetupResponse(BaseModel):
    secret: str
    qr_code: str


class TotpVerify(BaseModel):
    code: str
    temp_token: Optional[str] = None
    secret: Optional[str] = None


# ======================================================
# TARIFF SCHEMAS
# ======================================================

class TariffSchema(BaseModel):
    id: Optional[int] = None
    name: str
    maintenance_repair: DecimalTariff
    social_rent: DecimalTariff
    heating: DecimalTariff
    water_heating: DecimalTariff
    water_supply: DecimalTariff
    sewage: DecimalTariff
    waste_disposal: DecimalTariff
    electricity_per_sqm: DecimalTariff
    electricity_rate: DecimalTariff
    # Дата вступления в силу (необязательная)
    effective_from: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


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

    model_config = ConfigDict(from_attributes=True)


# ======================================================
# ADJUSTMENT SCHEMAS
# ======================================================

class AdjustmentCreate(BaseModel):
    user_id: int
    amount: DecimalAmount
    description: str
    account_type: AllowedAccountType = "209"


class AdjustmentResponse(BaseModel):
    id: int
    amount: Decimal
    description: str
    account_type: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ======================================================
# READING SCHEMAS
# ======================================================

class ReadingSchema(BaseModel):
    hot_water: Decimal = Field(..., ge=0, le=99999, decimal_places=3)
    cold_water: Decimal = Field(..., ge=0, le=99999, decimal_places=3)
    electricity: Decimal = Field(..., ge=0, le=999999, decimal_places=3)


class ReadingStateResponse(BaseModel):
    period_name: Optional[str] = None

    prev_hot: Decimal
    prev_cold: Decimal
    prev_elect: Decimal

    current_hot: Optional[Decimal]
    current_cold: Optional[Decimal]
    current_elect: Optional[Decimal]

    total_cost: Optional[Decimal]

    total_209: Optional[Decimal] = None
    total_205: Optional[Decimal] = None

    is_draft: bool
    is_period_open: bool
    is_already_approved: bool = False

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
# ADMIN MANUAL READING SCHEMAS
# ======================================================

class AdminManualReadingSchema(BaseModel):
    user_id: int
    hot_water: Decimal = Field(..., ge=0, le=99999, decimal_places=3)
    cold_water: Decimal = Field(..., ge=0, le=99999, decimal_places=3)
    electricity: Decimal = Field(..., ge=0, le=999999, decimal_places=3)
    is_moving_out: bool = False
    total_days_in_month: int = Field(30, ge=1, le=31)
    days_lived: int = Field(30, ge=0, le=31)


class OneTimeChargeSchema(BaseModel):
    user_id: int
    amount: DecimalAmount
    description: str
    account_type: AllowedAccountType = "209"


# ======================================================
# DEVICE TOKEN SCHEMAS
# ======================================================

class DeviceTokenCreate(BaseModel):
    token: str


# ======================================================
# RELOCATE USER SCHEMA
# ======================================================

class RelocateUserSchema(BaseModel):
    new_room_id: Optional[int] = None
    charge_amount: Optional[DecimalAmount] = None
    charge_description: Optional[str] = None
    charge_account_type: AllowedAccountType = "209"
    is_eviction: bool = False


# ======================================================
# DEBT RESPONSE
# ======================================================

class UserDebtResponse(BaseModel):
    id: int
    username: str
    room: Optional[RoomResponse] = None
    debt_209: Optional[Decimal] = None
    overpayment_209: Optional[Decimal] = None
    debt_205: Optional[Decimal] = None
    overpayment_205: Optional[Decimal] = None
    total_cost: Optional[Decimal] = None

    model_config = ConfigDict(from_attributes=True)


class ReplaceMeterSchema(BaseModel):
    meter_type: str  # "hot", "cold", "elect"
    final_old_value: DecimalVolume
    initial_new_value: DecimalVolume
    new_serial: str