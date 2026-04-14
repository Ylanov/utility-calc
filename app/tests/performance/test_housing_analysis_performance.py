from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.utility.routers.rooms import analyze_housing
from app.tests.performance.helpers import (
    FakeExecuteResult,
    FakeRoom,
    FakeUser,
    SequencedAsyncSession,
    env_float,
    env_int,
    timed_async,
)


def _build_housing_dataset(room_count: int, user_count: int):
    rooms = []
    users = []

    for room_id in range(1, room_count + 1):
        rooms.append(
            FakeRoom(
                id=room_id,
                dormitory_name=f"Dorm {((room_id - 1) // 250) + 1}",
                room_number=str(100 + room_id),
                apartment_area=Decimal("0.00") if room_id % 40 == 0 else Decimal("18.50"),
                total_room_residents=2 + (room_id % 3),
            )
        )

    for user_id in range(1, user_count + 1):
        room_id = None if user_id % 25 == 0 else ((user_id - 1) % room_count) + 1
        users.append(
            FakeUser(
                id=user_id,
                username=f"resident_{user_id}",
                room_id=room_id,
                residents_count=1 + (user_id % 2),
            )
        )

    return rooms, users


@pytest.mark.perf
def test_analyze_housing_10k_users_under_budget():
    room_count = env_int("PERF_ANALYZE_ROOM_COUNT", 2_000)
    user_count = env_int("PERF_ANALYZE_USER_COUNT", 10_000)
    budget = env_float("PERF_ANALYZE_BUDGET_SECONDS", 5.0)

    rooms, users = _build_housing_dataset(room_count, user_count)
    db = SequencedAsyncSession(
        FakeExecuteResult(scalar_values=rooms),
        FakeExecuteResult(scalar_values=users),
    )

    duration, issues = timed_async(analyze_housing(db))

    assert len(issues["unattached_users"]) == user_count // 25
    assert issues["zero_area"], "Expected at least some zero-area rooms in the synthetic dataset"
    assert duration < budget, f"analyze_housing took {duration:.3f}s, budget={budget:.3f}s"


@pytest.mark.perf
def test_analyze_housing_scaling_stays_linear_enough():
    small_rooms, small_users = _build_housing_dataset(1_000, 5_000)
    large_rooms, large_users = _build_housing_dataset(2_000, 10_000)

    small_db = SequencedAsyncSession(
        FakeExecuteResult(scalar_values=small_rooms),
        FakeExecuteResult(scalar_values=small_users),
    )
    large_db = SequencedAsyncSession(
        FakeExecuteResult(scalar_values=large_rooms),
        FakeExecuteResult(scalar_values=large_users),
    )

    small_duration, _ = timed_async(analyze_housing(small_db))
    large_duration, _ = timed_async(analyze_housing(large_db))

    scaling_ratio = large_duration / max(small_duration, 0.000001)
    assert scaling_ratio <= 8.0, (
        f"analyze_housing scaling is too steep: small={small_duration:.3f}s, "
        f"large={large_duration:.3f}s, ratio={scaling_ratio:.2f}"
    )
