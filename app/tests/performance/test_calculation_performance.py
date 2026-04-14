from __future__ import annotations

from decimal import Decimal

import pytest

from app.modules.utility.services.calculations import calculate_utilities
from app.tests.performance.helpers import FakeRoom, FakeTariff, FakeUser, env_float, env_int, timed_call


def _run_calculation_batch(iterations: int) -> Decimal:
    user = FakeUser(id=1, username="perf-user", residents_count=2)
    room = FakeRoom(id=1, apartment_area=Decimal("19.75"), total_room_residents=3)
    tariff = FakeTariff()
    total = Decimal("0.00")

    for idx in range(iterations):
        base = Decimal(idx % 17)
        result = calculate_utilities(
            user=user,
            room=room,
            tariff=tariff,
            volume_hot=Decimal("1.100") + (base / Decimal("100")),
            volume_cold=Decimal("2.250") + (base / Decimal("80")),
            volume_sewage=Decimal("3.350") + (base / Decimal("50")),
            volume_electricity_share=Decimal("55.000") + base,
            fraction=Decimal("1"),
        )
        total += result["total_cost"]

    return total


@pytest.mark.perf
def test_calculate_utilities_batch_10k_under_budget():
    iterations = env_int("PERF_CALC_ITERATIONS", 10_000)
    budget = env_float("PERF_CALC_BUDGET_SECONDS", 5.0)

    _run_calculation_batch(250)
    duration, total = timed_call(_run_calculation_batch, iterations)

    assert total > 0
    assert duration < budget, f"{iterations} calculations took {duration:.3f}s, budget={budget:.3f}s"


@pytest.mark.perf
def test_calculate_utilities_scaling_ratio_stays_near_linear():
    small = env_int("PERF_CALC_SMALL_BATCH", 1_000)
    large = env_int("PERF_CALC_LARGE_BATCH", 5_000)
    ratio_budget = env_float("PERF_CALC_MAX_SCALING_RATIO", 8.0)

    _run_calculation_batch(200)
    small_duration, small_total = timed_call(_run_calculation_batch, small)
    large_duration, large_total = timed_call(_run_calculation_batch, large)

    assert small_total > 0 and large_total > 0
    scaling_ratio = large_duration / max(small_duration, 0.000001)
    expected_ratio = large / small

    assert scaling_ratio <= min(ratio_budget, expected_ratio * 1.75), (
        f"Scaling ratio is too steep: small={small_duration:.3f}s, "
        f"large={large_duration:.3f}s, ratio={scaling_ratio:.2f}"
    )

