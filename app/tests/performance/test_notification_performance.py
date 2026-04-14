from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.modules.utility.services import notification_service
from app.tests.performance.helpers import FakeExecuteResult, SequencedAsyncSession, env_float, env_int, timed_async


def _success_response(tokens_count: int):
    return SimpleNamespace(
        success_count=tokens_count,
        failure_count=0,
        responses=[SimpleNamespace(success=True, exception=None) for _ in range(tokens_count)],
    )


@pytest.mark.perf
def test_send_push_to_all_batches_10k_tokens_by_500(monkeypatch):
    token_count = env_int("PERF_PUSH_TOKEN_COUNT", 10_000)
    budget = env_float("PERF_PUSH_BUDGET_SECONDS", 2.5)
    sent_batches = []

    def fake_send_each_multicast(message):
        sent_batches.append(len(message.tokens))
        return _success_response(len(message.tokens))

    monkeypatch.setattr(notification_service.firebase_admin, "_apps", ["perf-app"])
    monkeypatch.setattr(
        notification_service.messaging,
        "send_each_multicast",
        fake_send_each_multicast,
        raising=False,
    )

    db = SequencedAsyncSession(
        FakeExecuteResult(scalar_values=[f"token-{idx}" for idx in range(token_count)]),
    )

    duration, _ = timed_async(
        notification_service.send_push_to_all(
            db=db,
            title="Perf",
            body="Synthetic broadcast",
            data={"source": "pytest"},
        )
    )

    assert sent_batches
    assert all(batch <= 500 for batch in sent_batches)
    assert sum(sent_batches) == token_count
    assert duration < budget, f"send_push_to_all took {duration:.3f}s, budget={budget:.3f}s"


@pytest.mark.perf
def test_send_push_to_user_handles_multi_device_payload_fast(monkeypatch):
    token_count = env_int("PERF_PUSH_USER_DEVICE_COUNT", 50)
    budget = env_float("PERF_PUSH_USER_BUDGET_SECONDS", 1.0)
    seen_payload_sizes = []

    def fake_send_each_multicast(message):
        seen_payload_sizes.append(len(message.tokens))
        return _success_response(len(message.tokens))

    monkeypatch.setattr(notification_service.firebase_admin, "_apps", ["perf-app"])
    monkeypatch.setattr(
        notification_service.messaging,
        "send_each_multicast",
        fake_send_each_multicast,
        raising=False,
    )

    db = SequencedAsyncSession(
        FakeExecuteResult(scalar_values=[f"user-token-{idx}" for idx in range(token_count)]),
    )

    duration, result = timed_async(
        notification_service.send_push_to_user(
            db=db,
            user_id=1,
            title="Approved",
            body="Synthetic single-user notification",
            data={"scope": "single-user"},
        )
    )

    assert result["success_count"] == token_count
    assert seen_payload_sizes == [token_count]
    assert duration < budget, f"send_push_to_user took {duration:.3f}s, budget={budget:.3f}s"
