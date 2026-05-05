"""Tests for pergb endpoint surface — auth gate + admin force-poll stub.

Real handler behavior (reserve / topup / traffic) is covered in
``test_endpoints_pergb.py``. This file just keeps the auth-required and
still-stubbed admin-poll contracts pinned.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "test-api-key")


@pytest.fixture
def _no_auth():
    from orchestrator.main import app, require_api_key

    app.dependency_overrides[require_api_key] = lambda: None
    yield
    app.dependency_overrides.pop(require_api_key, None)


def test_admin_traffic_poll_still_stub_returns_501(_no_auth: None) -> None:
    """Admin force-poll lands in B-8.3 — must remain 501 in B-8.2."""
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post("/v1/admin/traffic/poll")
    assert r.status_code == 501
    assert r.json()["error"] == "not_implemented"


def test_reserve_pergb_validates_request_body(_no_auth: None) -> None:
    """Pydantic validation runs before any service call → 422 on malformed body."""
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": "not-an-int"},
    )
    assert r.status_code == 422


def test_reserve_pergb_requires_auth() -> None:
    """No API key → 401 from the dependency gate, well before service logic."""
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 1, "gb_amount": 5},
    )
    assert r.status_code == 401


def test_topup_pergb_validates_request_body(_no_auth: None) -> None:
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/orders/ord_aaa/topup_pergb",
        json={"sku_id": "not-an-int"},
    )
    assert r.status_code == 422


def test_topup_pergb_requires_auth() -> None:
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/orders/ord_aaa/topup_pergb",
        json={"sku_id": 1, "gb_amount": 10},
    )
    assert r.status_code == 401


def test_traffic_requires_auth() -> None:
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/orders/ord_aaa/traffic")
    assert r.status_code == 401
