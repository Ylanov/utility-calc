from __future__ import annotations

import asyncio
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from time import perf_counter
from typing import Any, Sequence


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    return int(value)


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    return float(value)


def timed_call(func, *args, **kwargs):
    started = perf_counter()
    result = func(*args, **kwargs)
    return perf_counter() - started, result


def timed_async(coro):
    started = perf_counter()
    result = asyncio.run(coro)
    return perf_counter() - started, result


@dataclass
class FakeRoom:
    id: int
    dormitory_name: str = "Dorm 1"
    room_number: str = "101"
    apartment_area: Decimal = Decimal("18.50")
    total_room_residents: int = 2
    hw_meter_serial: str | None = None
    cw_meter_serial: str | None = None
    el_meter_serial: str | None = None


@dataclass
class FakeUser:
    id: int
    username: str
    room_id: int | None = 1
    residents_count: int = 1
    role: str = "user"
    is_deleted: bool = False
    hashed_password: str = "hash"
    workplace: str | None = None


@dataclass
class FakeTariff:
    water_supply: Decimal = Decimal("40.00")
    water_heating: Decimal = Decimal("150.00")
    sewage: Decimal = Decimal("35.00")
    electricity_rate: Decimal = Decimal("5.50")
    maintenance_repair: Decimal = Decimal("30.50")
    social_rent: Decimal = Decimal("5.10")
    waste_disposal: Decimal = Decimal("6.50")
    heating: Decimal = Decimal("25.00")
    electricity_per_sqm: Decimal = Decimal("1.20")


@dataclass
class FakePeriod:
    id: int
    name: str
    is_active: bool = True


@dataclass
class FakeAdjustment:
    amount: Decimal
    description: str
    account_type: str = "209"


@dataclass
class FakeReading:
    id: int
    room_id: int
    user_id: int | None = None
    period_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    hot_water: Decimal = Decimal("0.000")
    cold_water: Decimal = Decimal("0.000")
    electricity: Decimal = Decimal("0.000")
    debt_209: Decimal = Decimal("0.00")
    overpayment_209: Decimal = Decimal("0.00")
    debt_205: Decimal = Decimal("0.00")
    overpayment_205: Decimal = Decimal("0.00")
    total_209: Decimal = Decimal("0.00")
    total_205: Decimal = Decimal("0.00")
    total_cost: Decimal = Decimal("0.00")
    cost_hot_water: Decimal = Decimal("0.00")
    cost_cold_water: Decimal = Decimal("0.00")
    cost_electricity: Decimal = Decimal("0.00")
    cost_sewage: Decimal = Decimal("0.00")
    cost_maintenance: Decimal = Decimal("0.00")
    cost_social_rent: Decimal = Decimal("0.00")
    cost_waste: Decimal = Decimal("0.00")
    cost_fixed_part: Decimal = Decimal("0.00")
    hot_correction: Decimal = Decimal("0.000")
    cold_correction: Decimal = Decimal("0.000")
    electricity_correction: Decimal = Decimal("0.000")
    sewage_correction: Decimal = Decimal("0.000")
    anomaly_flags: str | None = None
    anomaly_score: int = 0
    is_approved: bool = False
    edit_count: int = 0
    edit_history: list[dict[str, Any]] = field(default_factory=list)


class FakeScalarAccessor:
    def __init__(self, values: Sequence[Any]):
        self._values = list(values)

    def all(self):
        return list(self._values)

    def first(self):
        return self._values[0] if self._values else None


class FakeExecuteResult:
    def __init__(
        self,
        *,
        rows: Sequence[Any] | None = None,
        scalar_values: Sequence[Any] | None = None,
        scalar_value: Any | None = None,
    ):
        self._rows = list(rows or [])
        self._scalar_values = list(scalar_values) if scalar_values is not None else None
        self._scalar_value = scalar_value

    def scalars(self):
        if self._scalar_values is not None:
            return FakeScalarAccessor(self._scalar_values)
        return FakeScalarAccessor(self._rows)

    def all(self):
        if self._rows:
            return list(self._rows)
        if self._scalar_values is not None:
            return list(self._scalar_values)
        return []

    def scalar_one(self):
        if self._scalar_value is not None:
            return self._scalar_value
        if self._scalar_values and len(self._scalar_values) == 1:
            return self._scalar_values[0]
        raise AssertionError("scalar_one() requested but no scalar value was configured")


class SequencedAsyncSession:
    def __init__(self, *results: FakeExecuteResult):
        self._results = deque(results)
        self.statements: list[Any] = []
        self.added: list[Any] = []
        self.deleted: list[Any] = []
        self.commit_calls = 0

    async def execute(self, statement):
        self.statements.append(statement)
        if self._results:
            return self._results.popleft()
        return FakeExecuteResult()

    async def commit(self):
        self.commit_calls += 1

    async def delete(self, obj):
        self.deleted.append(obj)

    def add(self, obj):
        self.added.append(obj)


class SequencedSyncSession:
    def __init__(self, *results: FakeExecuteResult):
        self._results = deque(results)
        self.statements: list[Any] = []
        self.added_batches: list[list[Any]] = []
        self.bulk_update_calls: list[tuple[Any, list[dict[str, Any]]]] = []
        self.commit_calls = 0
        self.rollback_calls = 0

    def execute(self, statement):
        self.statements.append(statement)
        if self._results:
            return self._results.popleft()
        return FakeExecuteResult()

    def add_all(self, objects):
        self.added_batches.append(list(objects))

    def bulk_update_mappings(self, model, mappings):
        self.bulk_update_calls.append((model, list(mappings)))

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        self.rollback_calls += 1
