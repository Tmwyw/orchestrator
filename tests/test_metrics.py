"""Tests for Prometheus metrics module and HTTP middleware."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "test-api-key")
    os.environ.setdefault("ORCHESTRATOR_API_KEY", "test-api-key")


def test_metrics_endpoint_no_auth() -> None:
    """/metrics returns 200 without X-NETRUN-API-KEY header."""
    from fastapi.testclient import TestClient

    from orchestrator.main import app

    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "netrun_" in response.text


def test_http_middleware_increments_counter() -> None:
    """HTTP middleware bumps HTTP_REQUESTS counter on each request."""
    from fastapi.testclient import TestClient

    from orchestrator.main import app
    from orchestrator.metrics import HTTP_REQUESTS

    client = TestClient(app)
    before = HTTP_REQUESTS.labels(method="GET", path="/health", status="401")._value.get()
    client.get("/health")
    after = HTTP_REQUESTS.labels(method="GET", path="/health", status="401")._value.get()
    assert after == before + 1


def test_watchdog_actions_counter() -> None:
    """Mapping watchdog counters dict to WATCHDOG_ACTIONS works correctly."""
    from orchestrator.metrics import WATCHDOG_ACTIONS

    counters = {
        "jobs_failed_running": 3,
        "orders_released_expired": 2,
        "inventory_invalidated_stale": 5,
        "delivery_content_expired": 1,
    }
    before = {k: WATCHDOG_ACTIONS.labels(action=k)._value.get() for k in counters}
    for action, n in counters.items():
        if n:
            WATCHDOG_ACTIONS.labels(action=action).inc(n)
    for action, n in counters.items():
        after = WATCHDOG_ACTIONS.labels(action=action)._value.get()
        assert after == before[action] + n


def test_scheduler_run_counter_labels() -> None:
    """SCHEDULER_RUN_TOTAL accepts both success and failed status labels."""
    from orchestrator.metrics import SCHEDULER_RUN_TOTAL

    before_ok = SCHEDULER_RUN_TOTAL.labels(scheduler="refill", status="success")._value.get()
    before_fail = SCHEDULER_RUN_TOTAL.labels(scheduler="refill", status="failed")._value.get()
    SCHEDULER_RUN_TOTAL.labels(scheduler="refill", status="success").inc()
    SCHEDULER_RUN_TOTAL.labels(scheduler="refill", status="failed").inc()
    assert SCHEDULER_RUN_TOTAL.labels(scheduler="refill", status="success")._value.get() == before_ok + 1
    assert SCHEDULER_RUN_TOTAL.labels(scheduler="refill", status="failed")._value.get() == before_fail + 1
