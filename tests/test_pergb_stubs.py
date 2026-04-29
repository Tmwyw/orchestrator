"""Tests for B-8.1 pay-per-GB stub endpoints — verify 501 contract."""

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


def test_reserve_pergb_returns_501(_no_auth: None) -> None:
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 1, "gb_amount": 5},
    )
    assert r.status_code == 501
    assert r.json()["error"] == "not_implemented"


def test_topup_pergb_returns_501(_no_auth: None) -> None:
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/orders/ord_aaa/topup_pergb",
        json={"sku_id": 1, "gb_amount": 10},
    )
    assert r.status_code == 501
    assert r.json()["error"] == "not_implemented"


def test_traffic_returns_501(_no_auth: None) -> None:
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/orders/ord_aaa/traffic")
    assert r.status_code == 501


def test_admin_traffic_poll_returns_501(_no_auth: None) -> None:
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post("/v1/admin/traffic/poll")
    assert r.status_code == 501


def test_reserve_pergb_validates_request_body(_no_auth: None) -> None:
    """Pydantic validation runs BEFORE 501 — invalid body → 422."""
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": "not-an-int"},
    )
    assert r.status_code == 422


def test_reserve_pergb_requires_auth() -> None:
    """Auth gate fires regardless of stub — no key → 401."""
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 1, "gb_amount": 5},
    )
    assert r.status_code == 401
