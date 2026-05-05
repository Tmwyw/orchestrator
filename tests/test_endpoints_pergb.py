"""Endpoint-level tests for B-8.2 pay-per-GB handlers.

Mocks ``PergbService`` methods to exercise the FastAPI wiring + error
mapping in ``orchestrator/pergb.py`` without touching DB/Redis. Service
internals (DB transactions, Redis idempotency, UNIQUE-violation Path B)
are covered separately in ``test_pergb_service.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from orchestrator.pergb_service import (
    ReservePergbResult,
    TopupPergbResult,
    TrafficResult,
)


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
def _mock_service(monkeypatch: pytest.MonkeyPatch):
    """Replace pergb_router's PergbService instance with mocks per call."""
    from orchestrator import pergb

    mock = type(
        "MockSvc",
        (),
        {
            "reserve_pergb": AsyncMock(),
            "topup_pergb": AsyncMock(),
            "get_traffic": AsyncMock(),
        },
    )()
    monkeypatch.setattr(pergb, "_service", mock)
    return mock


# ===== reserve_pergb =====


def test_reserve_pergb_happy_path(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    expires = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _mock_service.reserve_pergb.return_value = ReservePergbResult(
        success=True,
        order_ref="ord_abc",
        expires_at=expires,
        port=32001,
        host="2001:db8::1",
        login="u",
        password="p",
        bytes_quota=10 * 1024 * 1024 * 1024,
        price_amount=Decimal("9.50"),
    )

    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 5, "gb_amount": 10},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["order_ref"] == "ord_abc"
    assert body["port"] == 32001
    assert body["bytes_quota"] == 10 * 1024 * 1024 * 1024
    assert body["price_amount"] == "9.50"  # Decimal serialized as string per § 6.10


def test_reserve_pergb_invalid_tier(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.reserve_pergb.return_value = ReservePergbResult(
        success=False,
        error="invalid_tier_amount",
        available_tiers=[1, 3, 5, 10, 30],
    )
    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 5, "gb_amount": 7},
    )
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "invalid_tier_amount"
    assert body["available_tiers"] == [1, 3, 5, 10, 30]


def test_reserve_pergb_sku_not_pergb(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.reserve_pergb.return_value = ReservePergbResult(success=False, error="sku_not_pergb")
    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 5, "gb_amount": 10},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "sku_not_pergb"


def test_reserve_pergb_sku_not_found(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.reserve_pergb.return_value = ReservePergbResult(success=False, error="sku_not_found")
    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 99999, "gb_amount": 10},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "sku_not_found"


def test_reserve_pergb_insufficient_inventory(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.reserve_pergb.return_value = ReservePergbResult(
        success=False, error="insufficient_inventory"
    )
    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 5, "gb_amount": 10},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "insufficient_inventory"


def test_reserve_pergb_passes_idempotency_key_through(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    expires = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _mock_service.reserve_pergb.return_value = ReservePergbResult(
        success=True,
        order_ref="ord_xxx",
        expires_at=expires,
        port=32001,
        host="h",
        login="u",
        password="p",
        bytes_quota=1024,
        price_amount=Decimal("1.00"),
    )
    client = TestClient(app)
    client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 5, "gb_amount": 1, "idempotency_key": "K1"},
    )
    _mock_service.reserve_pergb.assert_awaited_once_with(
        user_id=1, sku_id=5, gb_amount=1, idempotency_key="K1"
    )


# ===== topup_pergb =====


def test_topup_pergb_happy_path(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    expires = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _mock_service.topup_pergb.return_value = TopupPergbResult(
        success=True,
        order_ref="ord_topup_xxx",
        parent_order_ref="ord_abc",
        topup_sequence=2,
        bytes_quota_total=20 * 1024 * 1024 * 1024,
        bytes_used=5 * 1024 * 1024 * 1024,
        expires_at=expires,
        price_amount=Decimal("9.50"),
        tier_price_per_gb=Decimal("0.95"),
    )

    client = TestClient(app)
    r = client.post(
        "/v1/orders/ord_abc/topup_pergb",
        json={"sku_id": 5, "gb_amount": 10},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["order_ref"] == "ord_topup_xxx"
    assert body["parent_order_ref"] == "ord_abc"
    assert body["topup_sequence"] == 2
    assert body["price_amount"] == "9.50"
    assert body["tier_price_per_gb"] == "0.95"


def test_topup_pergb_sku_mismatch(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.topup_pergb.return_value = TopupPergbResult(success=False, error="sku_mismatch_for_topup")
    client = TestClient(app)
    r = client.post(
        "/v1/orders/ord_abc/topup_pergb",
        json={"sku_id": 99, "gb_amount": 10},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "sku_mismatch_for_topup"


def test_topup_pergb_account_not_renewable(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.topup_pergb.return_value = TopupPergbResult(
        success=False, error="account_not_renewable", current_status="expired"
    )
    client = TestClient(app)
    r = client.post(
        "/v1/orders/ord_abc/topup_pergb",
        json={"sku_id": 5, "gb_amount": 10},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "account_not_renewable"
    assert body["current_status"] == "expired"


def test_topup_pergb_order_not_found(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.topup_pergb.return_value = TopupPergbResult(success=False, error="order_not_found")
    client = TestClient(app)
    r = client.post(
        "/v1/orders/ord_missing/topup_pergb",
        json={"sku_id": 5, "gb_amount": 10},
    )
    assert r.status_code == 404


def test_topup_pergb_invalid_tier_returns_available_list(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.topup_pergb.return_value = TopupPergbResult(
        success=False, error="invalid_tier_amount", available_tiers=[1, 3, 5, 10]
    )
    client = TestClient(app)
    r = client.post(
        "/v1/orders/ord_abc/topup_pergb",
        json={"sku_id": 5, "gb_amount": 7},
    )
    assert r.status_code == 400
    assert r.json()["available_tiers"] == [1, 3, 5, 10]


# ===== traffic =====


def test_traffic_happy_path(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    expires = datetime(2026, 6, 1, tzinfo=timezone.utc)
    polled = datetime(2026, 5, 1, tzinfo=timezone.utc)
    _mock_service.get_traffic.return_value = TrafficResult(
        success=True,
        order_ref="ord_abc",
        status="active",
        bytes_quota=10 * 1024 * 1024 * 1024,
        bytes_used=8 * 1024 * 1024 * 1024,
        bytes_remaining=2 * 1024 * 1024 * 1024,
        usage_pct=0.8,
        last_polled_at=polled,
        expires_at=expires,
        depleted_at=None,
        node_id="node-x",
        port=32001,
        over_usage_bytes=0,
    )

    client = TestClient(app)
    r = client.get("/v1/orders/ord_abc/traffic")

    assert r.status_code == 200
    body = r.json()
    assert body["order_ref"] == "ord_abc"
    assert body["status"] == "active"
    assert body["usage_pct"] == 0.8
    assert body["over_usage_bytes"] == 0
    assert body["port"] == 32001


def test_traffic_top_up_order_returns_helpful_404(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.get_traffic.return_value = TrafficResult(
        success=False,
        error="traffic_account_not_found",
        detail="this is a top-up order; use the parent order_ref",
    )
    client = TestClient(app)
    r = client.get("/v1/orders/ord_topup_xxx/traffic")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "traffic_account_not_found"
    assert "parent" in body["detail"]


def test_traffic_order_not_found(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.get_traffic.return_value = TrafficResult(success=False, error="order_not_found")
    client = TestClient(app)
    r = client.get("/v1/orders/ord_missing/traffic")
    assert r.status_code == 404


def test_traffic_over_usage_serializes(_no_auth: None, _mock_service) -> None:
    """When bytes_used > bytes_quota, usage_pct stays capped at 1.0 and
    over_usage_bytes is positive."""
    from orchestrator.main import app

    expires = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _mock_service.get_traffic.return_value = TrafficResult(
        success=True,
        order_ref="ord_x",
        status="depleted",
        bytes_quota=1000,
        bytes_used=1100,
        bytes_remaining=0,
        usage_pct=1.0,
        last_polled_at=None,
        expires_at=expires,
        depleted_at=expires,
        node_id="n",
        port=32002,
        over_usage_bytes=100,
    )
    client = TestClient(app)
    r = client.get("/v1/orders/ord_x/traffic")
    assert r.status_code == 200
    body = r.json()
    assert body["usage_pct"] == 1.0
    assert body["over_usage_bytes"] == 100
    assert body["bytes_remaining"] == 0
