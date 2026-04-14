from __future__ import annotations

from decimal import Decimal

import pytest
from openpyxl import Workbook

from app.modules.utility.services.debt_import import sync_import_debts_process
from app.tests.performance.helpers import (
    FakeExecuteResult,
    FakePeriod,
    FakeReading,
    SequencedSyncSession,
    env_float,
    env_int,
    timed_call,
)


def _write_debt_workbook(path, row_count: int):
    workbook = Workbook()
    sheet = workbook.active

    for _ in range(7):
        sheet.append([])

    for idx in range(1, row_count + 1):
        sheet.append(
            [
                f"Resident {idx}",
                None,
                None,
                None,
                None,
                float(100 + idx),
                float(idx % 9),
            ]
        )

    workbook.save(path)
    workbook.close()


def _build_users(row_count: int):
    return [
        type(
            "UserStub",
            (),
            {
                "id": idx,
                "username": f"Resident {idx}",
                "room_id": idx,
                "is_deleted": False,
            },
        )()
        for idx in range(1, row_count + 1)
    ]


def _build_existing_readings(row_count: int):
    return [
        FakeReading(
            id=idx,
            room_id=idx,
            user_id=idx,
            period_id=1,
            debt_209=Decimal("0.00"),
            overpayment_209=Decimal("0.00"),
            debt_205=Decimal("0.00"),
            overpayment_205=Decimal("0.00"),
        )
        for idx in range(1, row_count + 1)
    ]


@pytest.mark.perf
def test_debt_import_updates_existing_room_readings_under_budget(tmp_path):
    row_count = env_int("PERF_DEBT_IMPORT_ROWS", 3_000)
    budget = env_float("PERF_DEBT_IMPORT_BUDGET_SECONDS", 8.0)
    workbook_path = tmp_path / "debts_existing.xlsx"
    _write_debt_workbook(workbook_path, row_count)

    db = SequencedSyncSession(
        FakeExecuteResult(scalar_values=[FakePeriod(id=1, name="April 2026")]),
        FakeExecuteResult(scalar_values=_build_users(row_count)),
        FakeExecuteResult(scalar_values=_build_existing_readings(row_count)),
    )

    duration, stats = timed_call(sync_import_debts_process, str(workbook_path), db, "209")

    assert stats["processed"] == row_count
    assert stats["updated"] == row_count
    assert stats["created"] == 0
    assert db.commit_calls == 1
    assert db.bulk_update_calls
    assert duration < budget, f"existing-reading import took {duration:.3f}s, budget={budget:.3f}s"


@pytest.mark.perf
def test_debt_import_creates_missing_readings_in_bulk(tmp_path):
    row_count = env_int("PERF_DEBT_IMPORT_CREATE_ROWS", 1_500)
    budget = env_float("PERF_DEBT_IMPORT_CREATE_BUDGET_SECONDS", 6.0)
    workbook_path = tmp_path / "debts_new.xlsx"
    _write_debt_workbook(workbook_path, row_count)

    db = SequencedSyncSession(
        FakeExecuteResult(scalar_values=[FakePeriod(id=1, name="April 2026")]),
        FakeExecuteResult(scalar_values=_build_users(row_count)),
        FakeExecuteResult(scalar_values=[]),
    )

    duration, stats = timed_call(sync_import_debts_process, str(workbook_path), db, "205")

    assert stats["processed"] == row_count
    assert stats["created"] == row_count
    assert db.commit_calls == 1
    assert db.added_batches and len(db.added_batches[0]) == row_count
    assert duration < budget, f"new-reading import took {duration:.3f}s, budget={budget:.3f}s"
