from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.tests.performance.helpers import (
    FakeAdjustment,
    FakePeriod,
    FakeReading,
    FakeRoom,
    FakeTariff,
    FakeUser,
    env_float,
    env_int,
    timed_call,
)

try:
    from app.modules.utility.services.pdf_generator import generate_receipt_pdf
except OSError as exc:  # pragma: no cover - depends on local native WeasyPrint libs
    pytest.skip(f"WeasyPrint native libraries are unavailable: {exc}", allow_module_level=True)


def _build_receipt_payload(index: int):
    room = FakeRoom(
        id=index,
        dormitory_name="Dorm A",
        room_number=str(100 + index),
        apartment_area=Decimal("18.50"),
        total_room_residents=3,
    )
    user = FakeUser(
        id=index,
        username=f"Resident {index}",
        room_id=room.id,
        residents_count=2,
    )
    tariff = FakeTariff()
    period = FakePeriod(id=1, name="April 2026")
    previous = FakeReading(
        id=1_000 + index,
        room_id=room.id,
        user_id=user.id,
        period_id=period.id - 1,
        hot_water=Decimal("100.000"),
        cold_water=Decimal("200.000"),
        electricity=Decimal("300.000"),
        created_at=datetime(2026, 3, 31, tzinfo=timezone.utc),
        is_approved=True,
    )
    reading = FakeReading(
        id=2_000 + index,
        room_id=room.id,
        user_id=user.id,
        period_id=period.id,
        created_at=datetime(2026, 4, 30, tzinfo=timezone.utc),
        hot_water=Decimal("103.250"),
        cold_water=Decimal("207.400"),
        electricity=Decimal("332.100"),
        debt_209=Decimal("10.00"),
        overpayment_209=Decimal("0.00"),
        debt_205=Decimal("0.00"),
        overpayment_205=Decimal("0.00"),
        total_209=Decimal("1750.55"),
        total_205=Decimal("120.00"),
        total_cost=Decimal("1870.55"),
        cost_hot_water=Decimal("617.50"),
        cost_cold_water=Decimal("296.00"),
        cost_sewage=Decimal("379.75"),
        cost_electricity=Decimal("176.55"),
        cost_maintenance=Decimal("564.25"),
        cost_social_rent=Decimal("94.35"),
        cost_waste=Decimal("120.25"),
        cost_fixed_part=Decimal("217.90"),
    )
    adjustments = [
        FakeAdjustment(
            amount=Decimal("15.50"),
            description="Synthetic recalc",
            account_type="209",
        )
    ]
    return user, room, reading, period, tariff, previous, adjustments


@pytest.mark.perf
@pytest.mark.slow
def test_generate_single_receipt_pdf_under_budget(tmp_path):
    budget = env_float("PERF_PDF_SINGLE_BUDGET_SECONDS", 12.0)
    payload = _build_receipt_payload(1)

    duration, filepath = timed_call(
        generate_receipt_pdf,
        user=payload[0],
        room=payload[1],
        reading=payload[2],
        period=payload[3],
        tariff=payload[4],
        prev_reading=payload[5],
        adjustments=payload[6],
        output_dir=str(tmp_path),
    )

    assert os.path.exists(filepath)
    assert os.path.getsize(filepath) > 1_000
    assert duration < budget, f"single receipt PDF took {duration:.3f}s, budget={budget:.3f}s"


@pytest.mark.perf
@pytest.mark.slow
def test_generate_small_pdf_batch_under_budget(tmp_path):
    batch_size = env_int("PERF_PDF_BATCH_SIZE", 3)
    budget = env_float("PERF_PDF_BATCH_BUDGET_SECONDS", 25.0)

    def run_batch():
        generated = []
        for idx in range(1, batch_size + 1):
            payload = _build_receipt_payload(idx)
            generated.append(
                generate_receipt_pdf(
                    user=payload[0],
                    room=payload[1],
                    reading=payload[2],
                    period=payload[3],
                    tariff=payload[4],
                    prev_reading=payload[5],
                    adjustments=payload[6],
                    output_dir=str(tmp_path),
                )
            )
        return generated

    duration, filepaths = timed_call(run_batch)

    assert len(filepaths) == batch_size
    assert all(os.path.exists(path) for path in filepaths)
    assert duration < budget, f"{batch_size} PDFs took {duration:.3f}s, budget={budget:.3f}s"
