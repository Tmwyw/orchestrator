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
    GeneratedPortRow,
    GeneratePortsResult,
    PergbBatchSummary,
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
            "generate_ports": AsyncMock(),
            "list_batches": AsyncMock(),
            "list_batch_ports": AsyncMock(),
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
        bytes_quota=10 * 1024 * 1024 * 1024,
        price_amount=Decimal("9.50"),
        traffic_account_id=42,
    )

    client = TestClient(app)
    r = client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 5, "gb_amount": 10},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["order_ref"] == "ord_abc"
    assert body["traffic_account_id"] == 42
    assert body["bytes_quota"] == 10 * 1024 * 1024 * 1024
    assert body["price_amount"] == "9.50"  # Decimal serialized as string per § 6.10
    # Wave PERGB-RFCT-A: reserve no longer returns port credentials.
    assert "port" not in body
    assert "host" not in body


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


# Wave PERGB-RFCT-A: reserve_pergb no longer touches the inventory pool — port
# allocation moved to /v1/pergb/{ref}/generate_ports. The "insufficient_inventory"
# error path was removed at the source; pool exhaustion now surfaces as
# "insufficient_pool" on generate_ports (covered below).


def test_reserve_pergb_passes_idempotency_key_through(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    expires = datetime(2026, 6, 1, tzinfo=timezone.utc)
    _mock_service.reserve_pergb.return_value = ReservePergbResult(
        success=True,
        order_ref="ord_xxx",
        expires_at=expires,
        bytes_quota=1024,
        price_amount=Decimal("1.00"),
        traffic_account_id=7,
    )
    client = TestClient(app)
    client.post(
        "/v1/orders/reserve_pergb",
        json={"user_id": 1, "sku_id": 5, "gb_amount": 1, "idempotency_key": "K1"},
    )
    _mock_service.reserve_pergb.assert_awaited_once_with(
        user_id=1, sku_id=5, gb_amount=1, idempotency_key="K1"
    )


# ===== generate_ports =====


def test_generate_ports_happy_path(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.generate_ports.return_value = GeneratePortsResult(
        success=True,
        order_ref="ord_abc",
        traffic_account_id=42,
        ports=[
            GeneratedPortRow(port=32001, host="2001:db8::1", login="u1", password="p1", geo_code="us"),
            GeneratedPortRow(port=32002, host="2001:db8::1", login="u2", password="p2", geo_code="us"),
        ],
        total_ports_for_client=2,
    )

    client = TestClient(app)
    r = client.post(
        "/v1/pergb/ord_abc/generate_ports",
        json={"count": 2, "geo_code": "us", "idempotency_key": "k-generate-1"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["order_ref"] == "ord_abc"
    assert body["traffic_account_id"] == 42
    assert body["total_ports_for_client"] == 2
    assert len(body["ports"]) == 2
    assert body["ports"][0]["port"] == 32001
    assert body["ports"][0]["login"] == "u1"


def test_generate_ports_order_not_found(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.generate_ports.return_value = GeneratePortsResult(success=False, error="order_not_found")
    client = TestClient(app)
    r = client.post(
        "/v1/pergb/ord_missing/generate_ports",
        json={"count": 1, "geo_code": "us", "idempotency_key": "k-generate-2"},
    )
    assert r.status_code == 404
    assert r.json()["error"] == "order_not_found"


def test_generate_ports_account_not_active(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.generate_ports.return_value = GeneratePortsResult(
        success=False, error="account_not_active", current_status="depleted"
    )
    client = TestClient(app)
    r = client.post(
        "/v1/pergb/ord_abc/generate_ports",
        json={"count": 1, "geo_code": "us", "idempotency_key": "k-generate-3"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "account_not_active"
    assert body["current_status"] == "depleted"


def test_generate_ports_insufficient_pool(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.generate_ports.return_value = GeneratePortsResult(
        success=False,
        error="insufficient_pool",
        requested=10,
        available=3,
        geo_code="us",
    )
    client = TestClient(app)
    r = client.post(
        "/v1/pergb/ord_abc/generate_ports",
        json={"count": 10, "geo_code": "us", "idempotency_key": "k-generate-4"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["error"] == "insufficient_pool"
    assert body["available"] == 3
    assert body["requested"] == 10
    assert body["geo_code"] == "us"


def test_generate_ports_passes_idempotency_key_through(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.generate_ports.return_value = GeneratePortsResult(
        success=True,
        order_ref="ord_abc",
        traffic_account_id=42,
        ports=[],
        total_ports_for_client=0,
    )
    client = TestClient(app)
    client.post(
        "/v1/pergb/ord_abc/generate_ports",
        json={"count": 1, "geo_code": "us", "idempotency_key": "k-pass-through"},
    )
    _mock_service.generate_ports.assert_awaited_once_with(
        order_ref="ord_abc", count=1, geo_code="us", idempotency_key="k-pass-through"
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


# ===== list_batches / list_batch_ports (per-generation re-download) =====


def test_list_batches_happy_path(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    ts1 = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 5, 14, 13, 0, tzinfo=timezone.utc)
    _mock_service.list_batches.return_value = [
        PergbBatchSummary(batch_id="abc123", geo_code="us", count=5, created_at=ts1),
        PergbBatchSummary(batch_id="def456", geo_code="de", count=3, created_at=ts2),
    ]
    client = TestClient(app)
    r = client.get("/v1/pergb/ord_abc/batches")

    assert r.status_code == 200
    body = r.json()
    assert body["order_ref"] == "ord_abc"
    assert body["total"] == 2
    assert body["batches"][0] == {
        "batch_id": "abc123",
        "geo_code": "us",
        "count": 5,
        "created_at": ts1.isoformat(),
    }
    assert body["batches"][1]["batch_id"] == "def456"
    _mock_service.list_batches.assert_awaited_once_with(order_ref="ord_abc")


def test_list_batches_empty(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.list_batches.return_value = []
    client = TestClient(app)
    r = client.get("/v1/pergb/ord_abc/batches")

    assert r.status_code == 200
    body = r.json()
    assert body["batches"] == []
    assert body["total"] == 0


def test_list_batches_order_not_found(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.list_batches.return_value = None
    client = TestClient(app)
    r = client.get("/v1/pergb/ord_missing/batches")

    assert r.status_code == 404
    assert r.json()["error"] == "order_not_found"


def test_list_batch_ports_happy_path(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.list_batch_ports.return_value = [
        GeneratedPortRow(port=32001, host="2001:db8::1", login="u1", password="p1", geo_code="us"),
        GeneratedPortRow(port=32002, host="2001:db8::1", login="u2", password="p2", geo_code="us"),
    ]
    client = TestClient(app)
    r = client.get("/v1/pergb/ord_abc/batches/abc123/ports")

    assert r.status_code == 200
    body = r.json()
    assert body["order_ref"] == "ord_abc"
    assert body["batch_id"] == "abc123"
    assert body["total"] == 2
    assert body["ports"][0] == {
        "port": 32001,
        "host": "2001:db8::1",
        "login": "u1",
        "password": "p1",
        "geo_code": "us",
    }
    _mock_service.list_batch_ports.assert_awaited_once_with(order_ref="ord_abc", batch_id="abc123")


def test_list_batch_ports_not_found(_no_auth: None, _mock_service) -> None:
    from orchestrator.main import app

    _mock_service.list_batch_ports.return_value = None
    client = TestClient(app)
    r = client.get("/v1/pergb/ord_abc/batches/missing/ports")

    assert r.status_code == 404
    assert r.json()["error"] == "batch_not_found"
