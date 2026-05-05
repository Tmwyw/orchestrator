"""Tests for the B-8.3 admin force-poll endpoint /v1/admin/traffic/poll."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from orchestrator.traffic_poll import PollCounters


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "test-api-key")


@pytest.fixture
def _no_auth():
    from orchestrator.main import app, require_api_key

    app.dependency_overrides[require_api_key] = lambda: None
    yield
    app.dependency_overrides.pop(require_api_key, None)


@pytest.fixture
def _mock_service(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the module-level TrafficPollService.run_once with a MagicMock."""
    from orchestrator import admin

    mock = MagicMock(name="run_once")
    monkeypatch.setattr(admin._traffic_poll_service, "run_once", mock)
    return mock


def test_admin_force_poll_no_filter_returns_counters(_no_auth: None, _mock_service: MagicMock) -> None:
    from orchestrator.main import app

    _mock_service.return_value = PollCounters(
        accounts_polled=4,
        accounts_depleted=1,
        accounts_disabled=1,
        node_failures=0,
        counter_resets_detected=0,
        nodes_polled=2,
        bytes_observed_total=1_234_567,
    )
    client = TestClient(app)
    r = client.post("/v1/admin/traffic/poll")

    assert r.status_code == 200
    body = r.json()
    assert body == {
        "accounts_polled": 4,
        "nodes_polled": 2,
        "bytes_observed_total": 1_234_567,
        "counter_resets_detected": 0,
        "accounts_marked_depleted": 1,
    }
    # No filters passed — both kwargs are None.
    _mock_service.assert_called_once_with(node_id_filter=None, account_id_filter=None)


def test_admin_force_poll_node_id_filter(_no_auth: None, _mock_service: MagicMock) -> None:
    from orchestrator.main import app

    _mock_service.return_value = PollCounters(
        accounts_polled=2,
        nodes_polled=1,
        bytes_observed_total=500,
    )
    client = TestClient(app)
    r = client.post("/v1/admin/traffic/poll?node_id=node-x")

    assert r.status_code == 200
    assert r.json()["nodes_polled"] == 1
    _mock_service.assert_called_once_with(node_id_filter="node-x", account_id_filter=None)


def test_admin_force_poll_account_id_filter(_no_auth: None, _mock_service: MagicMock) -> None:
    from orchestrator.main import app

    _mock_service.return_value = PollCounters(
        accounts_polled=1,
        nodes_polled=1,
        bytes_observed_total=42,
    )
    client = TestClient(app)
    r = client.post("/v1/admin/traffic/poll?account_id=99")

    assert r.status_code == 200
    assert r.json()["accounts_polled"] == 1
    _mock_service.assert_called_once_with(node_id_filter=None, account_id_filter=99)


def test_admin_force_poll_both_filters(_no_auth: None, _mock_service: MagicMock) -> None:
    from orchestrator.main import app

    _mock_service.return_value = PollCounters(accounts_polled=1, nodes_polled=1, bytes_observed_total=10)
    client = TestClient(app)
    r = client.post("/v1/admin/traffic/poll?node_id=node-x&account_id=99")

    assert r.status_code == 200
    _mock_service.assert_called_once_with(node_id_filter="node-x", account_id_filter=99)


def test_admin_force_poll_invalid_account_id_returns_422(_no_auth: None, _mock_service: MagicMock) -> None:
    """account_id is typed int — non-numeric query value rejected at FastAPI layer."""
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post("/v1/admin/traffic/poll?account_id=not-an-int")
    assert r.status_code == 422
    _mock_service.assert_not_called()


def test_admin_force_poll_skipped_overlap_propagates(_no_auth: None, _mock_service: MagicMock) -> None:
    """When the in-process lock is held (skipped_overlap path), counters are
    all zero — the response shape stays valid (no special status code)."""
    from orchestrator.main import app

    _mock_service.return_value = PollCounters(skipped_overlap=True)
    client = TestClient(app)
    r = client.post("/v1/admin/traffic/poll")

    assert r.status_code == 200
    body = r.json()
    assert body["accounts_polled"] == 0
    assert body["bytes_observed_total"] == 0


def test_admin_force_poll_requires_auth() -> None:
    """No X-NETRUN-API-KEY → 401 at the dependency gate, before the handler runs."""
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post("/v1/admin/traffic/poll")
    assert r.status_code == 401
