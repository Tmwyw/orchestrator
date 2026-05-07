"""Tests for GET /v1/skus/active — public SKU catalog for bot."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

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


def _wire_fetch_all(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]) -> list[str]:
    """Replace orchestrator.main.fetch_all with a fake returning ``rows``.

    Returns the list of captured query strings for assertions.
    """
    seen: list[str] = []

    def fake_fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
        seen.append(query)
        return rows

    monkeypatch.setattr("orchestrator.main.fetch_all", fake_fetch_all)
    return seen


def test_skus_active_empty(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    """No active SKUs → items=[], count=0."""
    _wire_fetch_all(monkeypatch, [])

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/skus/active")

    assert r.status_code == 200
    body = r.json()
    assert body == {"success": True, "items": [], "count": 0}


def test_skus_active_renders_per_piece_ipv6(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    _wire_fetch_all(
        monkeypatch,
        [
            {
                "sku_id": 1,
                "code": "ipv6_jp",
                "geo_code": "JP",
                "product_kind": "ipv6",
                "duration_days": 30,
                "price_per_piece": Decimal("1.50"),
                "price_per_gb": None,
                "stock_available": 42,
            }
        ],
    )

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/skus/active")

    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["sku_id"] == 1
    assert item["code"] == "ipv6_jp"
    assert item["geo_code"] == "JP"
    assert item["product_kind"] == "ipv6_per_piece"
    assert item["name"] == "IPv6 SOCKS5"
    assert item["price_per_unit"] == "1.50"
    assert item["stock_available"] == 42
    assert item["duration_days"] == 30
    assert item["tiers"] is None


def test_skus_active_renders_pergb_uses_price_per_gb(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    _wire_fetch_all(
        monkeypatch,
        [
            {
                "sku_id": 7,
                "code": "pergb_us",
                "geo_code": "US",
                "product_kind": "datacenter_pergb",
                "duration_days": 30,
                "price_per_piece": None,
                "price_per_gb": Decimal("0.95"),
                "stock_available": 0,
            }
        ],
    )

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/skus/active")

    body = r.json()
    item = body["items"][0]
    assert item["product_kind"] == "datacenter_pergb"
    assert item["name"] == "Pay-per-GB Datacenter"
    assert item["price_per_unit"] == "0.95"
    assert item["tiers"] is None


def test_skus_active_stock_query_filters_available(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    """The SQL must count proxy_inventory only where status='available' —
    not reserved/sold/expired_grace etc."""
    seen = _wire_fetch_all(monkeypatch, [])

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/skus/active")

    assert r.status_code == 200
    assert len(seen) == 1
    q = seen[0]
    assert "proxy_inventory" in q
    assert "status = 'available'" in q
    assert "is_active = TRUE" in q


def test_skus_active_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without X-Netrun-Api-Key header → 401."""
    _wire_fetch_all(monkeypatch, [])

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/skus/active")

    assert r.status_code == 401


def test_skus_active_orders_by_id(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    """Multiple SKUs come back in the order the SQL returns them (ORDER BY s.id)."""
    seen = _wire_fetch_all(
        monkeypatch,
        [
            {
                "sku_id": 1,
                "code": "ipv6_jp",
                "geo_code": "JP",
                "product_kind": "ipv6",
                "duration_days": 30,
                "price_per_piece": Decimal("1.00"),
                "price_per_gb": None,
                "stock_available": 5,
            },
            {
                "sku_id": 2,
                "code": "ipv6_us",
                "geo_code": "US",
                "product_kind": "ipv6",
                "duration_days": 30,
                "price_per_piece": Decimal("1.20"),
                "price_per_gb": None,
                "stock_available": 8,
            },
            {
                "sku_id": 3,
                "code": "pergb_us",
                "geo_code": "US",
                "product_kind": "datacenter_pergb",
                "duration_days": 30,
                "price_per_piece": None,
                "price_per_gb": Decimal("0.80"),
                "stock_available": 0,
            },
        ],
    )

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/skus/active")

    body = r.json()
    assert body["count"] == 3
    assert [i["sku_id"] for i in body["items"]] == [1, 2, 3]
    assert "ORDER BY s.id" in seen[0]


def test_skus_active_null_price_falls_back_to_zero(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    """If price_per_piece is NULL on an ipv6 SKU (misconfigured),
    we render 0 instead of crashing."""
    _wire_fetch_all(
        monkeypatch,
        [
            {
                "sku_id": 9,
                "code": "ipv6_xx",
                "geo_code": "XX",
                "product_kind": "ipv6",
                "duration_days": 30,
                "price_per_piece": None,
                "price_per_gb": None,
                "stock_available": 0,
            }
        ],
    )

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/skus/active")

    assert r.status_code == 200
    assert r.json()["items"][0]["price_per_unit"] == "0"
