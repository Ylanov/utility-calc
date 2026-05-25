# app/modules/utility/schemas.py

from pydantic import BaseModel, condecimal, Field, ConfigDict
from typing import Optional, List, Generic, TypeVar, Literal
from datetime import datetime, date
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
    # Тариф конкретной комнаты (опциональный). Если задан — у всех её жильцов
    # этот тариф побеждает их персональный (см. tariff_cache.get_effective_tariff).
    tariff_id: Optional[int] = None
    # Bug AS: холостяцкая квартира — счёт делится поровну между жильцами.
    is_singles_apartment: bool = False
    max_capacity: Optional[int] = None


class RoomUpdate(BaseModel):
    dormitory_name: Optional[str] = None
    room_number: Optional[str] = None
    apartment_area: Optional[DecimalAmount] = None
    total_room_residents: Optional[int] = None
    hw_meter_serial: Optional[str] = None
    cw_meter_serial: Optional[str] = None
    el_meter_serial: Optional[str] = None
    tariff_id: Optional[int] = None
    is_singles_apartment: Optional[bool] = None
    max_capacity: Optional[int] = None


class RoomResponse(BaseModel):
    id: int
    dormitory_name: str
    room_number: str
    apartment_area: DecimalAmount
    total_room_residents: int
    hw_meter_serial: Optional[str] = None
    cw_meter_serial: Optional[str] = None
    el_meter_serial: Optional[str] = None
    tariff_id: Optional[int] = None
    is_singles_apartment: bool = False
    max_capacity: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


# ======================================================
# USER SCHEMAS
# ======================================================

# В системе только 2 роли (см. миграцию roles_001_simplify, май 2026):
#   user — жилец общежития
#   admin — сотрудник с полными правами
# Раньше были accountant/financier — слиты в admin для упрощения.
AllowedRole = Literal["user", "admin"]
AllowedAccountType = Literal["209", "205"]


# Тип жильца (см. User.resident_type) — пара значений, валидация на уровне схемы.
ResidentType = Literal["family", "single"]
BillingMode = Literal["by_meter", "per_capita"]


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=8, max_length=128)
    role: AllowedRole = "user"
    workplace: str = ""
    residents_count: int = Field(1, ge=1, le=20)
    tariff_id: Optional[int] = None
    room_id: Optional[int] = None
    # 'family' (по счётчикам) | 'single' (койко-место)
    resident_type: ResidentType = "family"
    # 'by_meter' (как раньше) | 'per_capita' (фикс. сумма из тарифа)
    billing_mode: Optional[BillingMode] = None  # None → выводится из resident_type
    hw_meter_serial: Optional[str] = None
    cw_meter_serial: Optional[str] = None
    el_meter_serial: Optional[str] = None
    # Флаги наличия счётчиков. По умолчанию все True (как было).
    # Если False — анализатор не флагит «не подал X», в calculate_utilities
    # используется tariff.X_norm_per_capita × residents_count.
    has_hw_meter: bool = True
    has_cw_meter: bool = True
    has_el_meter: bool = True


class UserResponse(BaseModel):
    id: int
    username: str
    role: str

    workplace: Optional[str] = None
    residents_count: int

    tariff_id: Optional[int] = None
    resident_type: ResidentType = "family"
    billing_mode: BillingMode = "by_meter"

    is_2fa_enabled: bool = False
    is_initial_setup_done: bool = False

    has_hw_meter: bool = True
    has_cw_meter: bool = True
    has_el_meter: bool = True

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
    resident_type: Optional[ResidentType] = None
    billing_mode: Optional[BillingMode] = None
    hw_meter_serial: Optional[str] = None
    cw_meter_serial: Optional[str] = None
    el_meter_serial: Optional[str] = None
    has_hw_meter: Optional[bool] = None
    has_cw_meter: Optional[bool] = None
    has_el_meter: Optional[bool] = None


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
    # Bug AM: electricity_per_sqm (ОДН) убран из формулы расчёта в мае 2026.
    # Фронт его больше не отправляет, но колонка в БД осталась (default=0).
    # Делаем Optional с default=0 чтобы сохранение тарифа не падало 422.
    electricity_per_sqm: DecimalTariff = Decimal("0.00")
    electricity_rate: DecimalTariff
    # Фиксированная сумма за койко-место (для холостяков, billing_mode=per_capita).
    # 0 = тариф не предполагает одиночек.
    per_capita_amount: DecimalTariff = Decimal("0.00")
    # Нормативы потребления на 1 человека в месяц для случая когда
    # у жильца НЕТ счётчика (User.has_X_meter=False). Расход тогда:
    # v_X = norm_per_capita × residents_count. 0 = нет норматива → 0 потребление.
    # См. миграцию meters_001_per_user_config.
    hw_norm_per_capita: DecimalVolume = Decimal("0.000")
    cw_norm_per_capita: DecimalVolume = Decimal("0.000")
    el_norm_per_capita: DecimalVolume = Decimal("0.000")
    # Множитель норматива для не-подающих 4+ мес. (default 3.0). См.
    # tariffs_norm_001_coefficient.
    norm_coefficient: Decimal = Decimal("3.00")
    # Тип тарифа: 'family' / 'singles'. См. tariffs_type_001_family_singles.
    tariff_type: str = "family"
    # Дата вступления в силу (необязательная)
    effective_from: Optional[datetime] = None
    # ==============================================================
    # СЕЗОННОСТЬ per-tariff. См. миграцию tariffs_seasonal_002_per_tariff
    # и модель Tariff.is_heating_active_now / is_hw_heating_active_now.
    # heating_active — мастер-выключатель статьи «отопление» в тарифе.
    # heating_season_start/end — диапазон дат (год игнорируется). NULL = круглогодично.
    # ==============================================================
    heating_active: bool = True
    heating_season_start: Optional[date] = None
    heating_season_end: Optional[date] = None
    hw_heating_active: bool = True
    hw_heating_season_start: Optional[date] = None
    hw_heating_season_end: Optional[date] = None

    # Bug AS: skip-флаги для холостяцких квартир (Room.is_singles_apartment).
    # Все default False — поведение существующих тарифов не меняется.
    singles_skip_maintenance: bool = False
    singles_skip_social_rent: bool = False
    singles_skip_heating: bool = False
    singles_skip_waste: bool = False

    # Bug AT: глобальные флаги «что начисляет тариф». Default True —
    # zero-impact. Снять = статья не считается никому на этом тарифе.
    charge_hot_water: bool = True
    charge_cold_water: bool = True
    charge_sewage: bool = True
    charge_electricity: bool = True
    charge_maintenance: bool = True
    charge_social_rent: bool = True
    charge_heating: bool = True
    charge_waste: bool = True

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

    # Для UX: если жилец на per_capita — клиент скрывает форму ввода счётчиков
    # и показывает «к оплате X ₽ (койко-место по тарифу)».
    billing_mode: str = "by_meter"
    per_capita_amount: Optional[Decimal] = None  # фикс. сумма из тарифа жильца

    # Конфигурация счётчиков жильца (см. миграцию meters_001_per_user_config).
    # Если has_X_meter=False — мобилка/портал ПРЯЧЕТ соответствующее поле ввода,
    # сервер при расчёте берёт tariff.X_norm_per_capita × residents_count.
    # По умолчанию True (старое поведение — есть все три счётчика).
    has_hw_meter: bool = True
    has_cw_meter: bool = True
    has_el_meter: bool = True

    # Подсказка по формату ввода счётчиков (см. /api/settings/meter-format).
    # Мобилка/портал показывают example_hint и instructions под полем ввода,
    # чтобы жилец не вводил «01427957» вместо «01427.957» (см. инцидент
    # мая 2026 — 1.48 млрд ₽ на дашборде из-за пропущенных точек).
    meter_format_hint: Optional[str] = None
    meter_example: Optional[str] = None
    meter_instructions: Optional[str] = None


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
    # Раздельная подача (запрос мая 2026): админ может прислать только воду,
    # только электричество или всё вместе. Правило: ГВС+ХВС — оба или ни
    # одного (вода подаётся парой). Электричество — независимо.
    # Поля без значения трактуются как «не подавал» — в БД пишется
    # предыдущее значение, дельта = 0, расход не начисляется.
    # Валидация пары «вода-вместе» и «хоть что-то подано» — на сервере
    # в save_manual_entry.
    hot_water: Optional[Decimal] = Field(None, ge=0, le=99999, decimal_places=3)
    cold_water: Optional[Decimal] = Field(None, ge=0, le=99999, decimal_places=3)
    electricity: Optional[Decimal] = Field(None, ge=0, le=999999, decimal_places=3)
    is_moving_out: bool = False
    total_days_in_month: int = Field(30, ge=1, le=31)
    days_lived: int = Field(30, ge=0, le=31)
    # Опциональный целевой период. Если None — берётся active_period.
    # Запрос мая 2026: админу нужно вводить показания за прошлые месяцы
    # (когда жилец задним числом сообщил данные, или нужна коррекция).
    # Без поля админ был ограничен только текущим периодом.
    period_id: Optional[int] = None


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
