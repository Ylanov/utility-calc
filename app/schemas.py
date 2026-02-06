from pydantic import BaseModel, condecimal
from typing import Optional
from datetime import datetime
from decimal import Decimal

# =================================================================
# ОПРЕДЕЛЕНИЕ ТИПОВ ДАННЫХ (DECIMAL)
# =================================================================
# Используем condecimal для жесткой фиксации точности
# max_digits - общее количество цифр
# decimal_places - количество знаков после запятой

# Для денег: точность до копеек (2 знака)
DecimalAmount = condecimal(max_digits=12, decimal_places=2)

# Для объемов (вода, свет): точность до тысячных (3 знака)
DecimalVolume = condecimal(max_digits=12, decimal_places=3)

# Для тарифов: высокая точность для расчетов (4 знака)
DecimalTariff = condecimal(max_digits=10, decimal_places=4)


# =================================================================
# СХЕМЫ ПОЛЬЗОВАТЕЛЕЙ (USER)
# =================================================================
class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"
    dormitory: str = ""
    workplace: str = ""
    residents_count: int = 1
    total_room_residents: int = 1
    # Площадь важна для расчетов, используем Decimal
    apartment_area: DecimalAmount = Decimal("0.00")


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    dormitory: Optional[str]
    workplace: Optional[str]
    residents_count: int
    total_room_residents: int
    apartment_area: Decimal

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


# =================================================================
# СХЕМЫ ТАРИФОВ (TARIFF)
# =================================================================
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


# =================================================================
# СХЕМЫ ПЕРИОДОВ (PERIODS)
# =================================================================
class PeriodCreate(BaseModel):
    name: str  # Например: "Февраль 2025"


class PeriodResponse(BaseModel):
    id: int
    name: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


# =================================================================
# СХЕМЫ ПОКАЗАНИЙ (READINGS)
# =================================================================
class ReadingSchema(BaseModel):
    # Входные данные от пользователя - объемы
    hot_water: DecimalVolume
    cold_water: DecimalVolume
    electricity: DecimalVolume


class ReadingStateResponse(BaseModel):
    # Добавляем информацию о периоде
    period_name: Optional[str] = None

    # Объемы (предыдущие и текущие)
    prev_hot: Decimal
    prev_cold: Decimal
    prev_elect: Decimal

    current_hot: Optional[Decimal]
    current_cold: Optional[Decimal]
    current_elect: Optional[Decimal]

    # Итоговая сумма
    total_cost: Optional[Decimal]

    # Статусы
    is_draft: bool
    is_period_open: bool

    # Детализация стоимости (все поля денежные)
    cost_hot_water: Optional[Decimal] = None
    cost_cold_water: Optional[Decimal] = None
    cost_electricity: Optional[Decimal] = None
    cost_sewage: Optional[Decimal] = None
    cost_maintenance: Optional[Decimal] = None
    cost_social_rent: Optional[Decimal] = None
    cost_waste: Optional[Decimal] = None
    cost_fixed_part: Optional[Decimal] = None


class ApproveRequest(BaseModel):
    # Коррекции - это объемы, которые вычитаются/прибавляются
    hot_correction: DecimalVolume
    cold_correction: DecimalVolume
    electricity_correction: DecimalVolume
    sewage_correction: DecimalVolume