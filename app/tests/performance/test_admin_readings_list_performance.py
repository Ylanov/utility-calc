from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.modules.utility.services.admin_readings_list import get_paginated_readings
from app.tests.performance.helpers import (
    FakeExecuteResult,
    FakePeriod,
    FakeReading,
    FakeRoom,
    FakeUser,
    SequencedAsyncSession,
    env_float,
    env_int,
    timed_async,
)


def _build_page_rows(page_size: int):
    started = datetime(2026, 4, 1, tzinfo=timezone.utc)
    rows = []
    prev_readings = []

    for idx in range(1, page_size + 1):
        room = FakeRoom(
            id=idx,
            dormitory_name=f"Dorm {((idx - 1) // 50) + 1}",
            room_number=str(100 + idx),
            total_room_residents=3,
        )
        user = FakeUser(
            id=idx,
            username=f"user_{idx}",
            room_id=room.id,
            residents_count=1 + (idx % 2),
        )
        current = FakeReading(
            id=idx,
            room_id=room.id,
            user_id=user.id,
            period_id=1,
            hot_water=Decimal("100.100") + Decimal(idx),
            cold_water=Decimal("200.200") + Decimal(idx),
            electricity=Decimal("300.300") + Decimal(idx),
            total_cost=Decimal("123.45") + Decimal(idx),
            created_at=started + timedelta(minutes=idx),
            anomaly_flags="HIGH_HOT,HIGH_ELECT" if idx % 5 == 0 else None,
            anomaly_score=85 if idx % 5 == 0 else 10,
            edit_count=idx % 3,
            edit_history=[{"changed_by": "perf"}] if idx % 3 else [],
        )
        previous = FakeReading(
            id=10_000 + idx,
            room_id=room.id,
            user_id=user.id,
            period_id=0,
            hot_water=Decimal("99.100") + Decimal(idx),
            cold_water=Decimal("199.200") + Decimal(idx),
            electricity=Decimal("299.300") + Decimal(idx),
            created_at=started,
            is_approved=True,
        )
        rows.append((current, user, room))
        prev_readings.append(previous)

    return rows, prev_readings


@pytest.mark.perf
def test_admin_readings_page_serialization_under_budget():
    page_size = env_int("PERF_ADMIN_READINGS_PAGE_SIZE", 250)
    budget = env_float("PERF_ADMIN_READINGS_BUDGET_SECONDS", 2.0)
    rows, prev_readings = _build_page_rows(page_size)

    db = SequencedAsyncSession(
        FakeExecuteResult(scalar_values=[FakePeriod(id=1, name="April 2026")]),
        FakeExecuteResult(scalar_value=page_size),
        FakeExecuteResult(rows=rows),
        FakeExecuteResult(scalar_values=prev_readings),
    )

    duration, payload = timed_async(
        get_paginated_readings(
            db=db,
            page=1,
            limit=page_size,
            after_id=None,
            search=None,
            anomalies_only=False,
            sort_by="created_at",
            sort_dir="desc",
        )
    )

    assert payload["total"] == page_size
    assert len(payload["items"]) == page_size
    assert any(item["anomaly_details"] for item in payload["items"])
    assert duration < budget, f"admin readings serialization took {duration:.3f}s, budget={budget:.3f}s"
