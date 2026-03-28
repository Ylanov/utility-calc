from pydantic import BaseModel, condecimal, Field
from typing import Optional, List, Generic, TypeVar
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
# ROOM SCHEMAS (НОВОЕ)
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

    class Config:
        from_attributes = True

# ======================================================
# USER SCHEMAS (ОБНОВЛЕНО)
# ======================================================

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"
    workplace: str = ""
    residents_count: int = 1
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

    # 🔥 КЛЮЧЕВОЕ ИЗМЕНЕНИЕ
    room: Optional[RoomResponse] = None

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    role: Optional[str] = None
    workplace: Optional[str] = None
    residents_count: Optional[int] = None
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
    account_type: str = "209"


class AdjustmentResponse(BaseModel):
    id: int
    amount: Decimal
    description: str
    account_type: str
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
# FINANCIER RESPONSE (ОБНОВЛЕНО)
# ======================================================

class UserDebtResponse(BaseModel):
    id: int
    username: str

    # 🔥 теперь через комнату
    room: Optional[RoomResponse] = None

    debt_209: Decimal
    overpayment_209: Decimal

    debt_205: Decimal
    overpayment_205: Decimal

    current_total_cost: Decimal

    class Config:
        from_attributes = True


# ======================================================
# ADMIN MANUAL ENTRY SCHEMAS
# ======================================================

class AdminManualReadingSchema(BaseModel):
    user_id: int
    hot_water: DecimalVolume
    cold_water: DecimalVolume
    electricity: DecimalVolume


class OneTimeChargeSchema(BaseModel):
    user_id: int
    days_lived: int
    total_days_in_month: int
    hot_water: DecimalVolume
    cold_water: DecimalVolume
    electricity: DecimalVolume
    is_moving_out: bool = False

# ======================================================
# PUSH NOTIFICATIONS
# ======================================================
class DeviceTokenCreate(BaseModel):
    token: str
    device_type: str = "android"  # android или ios


# ======================================================
# MOVE & METER REPLACEMENT SCHEMAS
# ======================================================

class RelocateUserSchema(BaseModel):
    # Данные для расчета по старой комнате
    total_days_in_month: int
    days_lived: int
    hot_water: DecimalVolume
    cold_water: DecimalVolume
    electricity: DecimalVolume

    # Действие: 'move' (переселить) или 'evict' (выселить)
    action: str

    # Новая комната (обязательна только если action == 'move')
    new_room_id: Optional[int] = None


class ReplaceMeterSchema(BaseModel):
    meter_type: str  # "hot", "cold", "elect"
    final_old_value: DecimalVolume
    initial_new_value: DecimalVolume
    new_serial: str