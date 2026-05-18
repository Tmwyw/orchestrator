"""Tests for /v1/admin/skus, /v1/admin/skus/{id}/* (CATALOG-1 Phase A)."""

from __future__ import annotations

from datetime import datetime, timezone
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


# === GET /v1/admin/skus ===


def test_list_skus_returns_items_and_total(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    fake_rows = [
        {
            "id": 1,
            "code": "ipv6_us_socks5",
            "product_kind": "ipv6",
            "geo_code": "US",
            "protocol": "socks5",
            "duration_days": 30,
            "price_per_piece": Decimal("2.50"),
            "price_per_gb": None,
            "target_stock": 5000,
            "refill_batch_size": 500,
            "is_active": True,
            "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
            "stock_available": 4521,
        },
        {
            "id": 2,
            "code": "ipv6_uk_socks5",
            "product_kind": "ipv6",
            "geo_code": "UK",
            "protocol": "socks5",
            "duration_days": 30,
            "price_per_piece": Decimal("2.80"),
            "price_per_gb": None,
            "target_stock": 3000,
            "refill_batch_size": 500,
            "is_active": False,
            "created_at": datetime(2026, 4, 2, tzinfo=timezone.utc),
            "updated_at": datetime(2026, 4, 2, tzinfo=timezone.utc),
            "stock_available": 0,
        },
    ]

    def fake_fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
        assert "FROM skus s" in query
        return fake_rows

    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", fake_fetch_all)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    assert body["items"][0]["code"] == "ipv6_us_socks5"
    assert body["items"][0]["stock_available"] == 4521
    assert body["items"][0]["price_per_piece"] == "2.50"
    assert body["items"][1]["is_active"] is False


def test_list_skus_applies_filters(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
        captured["query"] = query
        captured["params"] = params
        return []

    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", fake_fetch_all)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus?kind=ipv6&geo=US&is_active=true")
    assert r.status_code == 200
    assert "s.product_kind = %s" in captured["query"]
    assert "s.geo_code = %s" in captured["query"]
    assert "s.is_active = %s" in captured["query"]
    assert "ipv6" in captured["params"]
    assert "US" in captured["params"]
    assert True in captured["params"]


def test_list_skus_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Note: no _no_auth fixture — require_api_key remains active.
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus")
    assert r.status_code == 401


# === GET /v1/admin/skus/{id} ===


def test_get_sku_returns_detail_with_breakdown(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    sku_row = {
        "id": 1,
        "code": "ipv6_us_socks5",
        "product_kind": "ipv6",
        "geo_code": "US",
        "protocol": "socks5",
        "duration_days": 30,
        "price_per_piece": Decimal("2.50"),
        "price_per_gb": None,
        "target_stock": 5000,
        "refill_batch_size": 500,
        "validation_require_ipv6": True,
        "is_active": True,
        "metadata": {},
        "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
    }
    breakdown_rows = [
        {
            "node_id": "node-us-1",
            "node_name": "node-us-1",
            "available": 2340,
            "reserved": 50,
            "sold": 6000,
            "expired_grace": 10,
            "pending_validation": 0,
        },
        {
            "node_id": "node-us-2",
            "node_name": "node-us-2",
            "available": 2181,
            "reserved": 0,
            "sold": 6340,
            "expired_grace": 0,
            "pending_validation": 5,
        },
    ]

    def fake_fetch_one(query: str, params: Any = None) -> dict[str, Any]:
        assert "FROM skus" in query
        assert params == (1,)
        return sku_row

    def fake_fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
        assert "sku_node_bindings" in query
        return breakdown_rows

    monkeypatch.setattr("orchestrator.admin_catalog.fetch_one", fake_fetch_one)
    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", fake_fetch_all)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus/1")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 1
    assert body["code"] == "ipv6_us_socks5"
    assert body["stock_total"]["available"] == 2340 + 2181
    assert body["stock_total"]["reserved"] == 50
    assert body["stock_total"]["sold"] == 12340
    assert body["stock_total"]["pending_validation"] == 5
    assert len(body["stock_breakdown"]) == 2
    assert body["stock_breakdown"][0]["node_id"] == "node-us-1"
    assert body["stock_breakdown"][0]["available"] == 2340


def test_get_sku_returns_404_when_missing(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    def fake_fetch_one(query: str, params: Any = None) -> dict[str, Any] | None:
        return None

    monkeypatch.setattr("orchestrator.admin_catalog.fetch_one", fake_fetch_one)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus/9999")
    assert r.status_code == 404
    body = r.json()
    assert body["error"] == "sku_not_found"
    assert body["success"] is False
