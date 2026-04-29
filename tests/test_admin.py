"""Tests for admin endpoints (/v1/admin/{stats,orders,archive})."""

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
    """Bypass require_api_key by overriding the dependency."""
    from orchestrator.main import app, require_api_key

    app.dependency_overrides[require_api_key] = lambda: None
    yield
    app.dependency_overrides.pop(require_api_key, None)


def test_admin_stats_returns_response_shape(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    sales = {"orders": 5, "proxies": 25, "revenue": Decimal("12.50")}
    inventory = [
        {"code": "ipv6_us_socks5", "status": "available", "n": 10},
        {"code": "ipv6_us_socks5", "status": "sold", "n": 15},
    ]
    nodes = {"ready": 3, "total": 3}

    def fake_fetch_one(query: str, params: Any = None) -> dict[str, Any]:
        if "from orders" in query:
            return sales
        if "from nodes" in query:
            return nodes
        raise AssertionError(f"unexpected fetch_one query: {query[:60]!r}")

    def fake_fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
        return inventory

    monkeypatch.setattr("orchestrator.admin.fetch_one", fake_fetch_one)
    monkeypatch.setattr("orchestrator.admin.fetch_all", fake_fetch_all)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/stats?range_days=7")
    assert r.status_code == 200
    body = r.json()
    assert body["sales"]["orders"] == 5
    assert body["sales"]["proxies"] == 25
    # Decimal -> string in mode="json"
    assert body["sales"]["revenue"] == "12.50"
    assert len(body["inventory"]) == 2
    assert body["nodes"] == {"ready": 3, "total": 3}


def test_admin_orders_filters_by_user_id(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch_all(query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        captured["query"] = query
        captured["params"] = params
        return [
            {
                "order_ref": "ord_aaa",
                "user_id": 42,
                "sku_id": 1,
                "status": "committed",
                "requested_count": 3,
                "allocated_count": 3,
                "reserved_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
                "expires_at": datetime(2026, 4, 1, 0, 5, tzinfo=timezone.utc),
                "committed_at": datetime(2026, 4, 1, 0, 1, tzinfo=timezone.utc),
                "proxies_expires_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
            },
        ]

    monkeypatch.setattr("orchestrator.admin.fetch_all", fake_fetch_all)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/orders?user_id=42")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["user_id"] == 42
    assert "user_id = %s" in captured["query"]
    assert 42 in captured["params"]


def test_admin_archive_filters_by_date_and_geo(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    captured: dict[str, Any] = {}

    def fake_fetch_all(query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        captured["query"] = query
        captured["params"] = params
        return []

    monkeypatch.setattr("orchestrator.admin.fetch_all", fake_fetch_all)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/archive?from_date=2026-01-01&to_date=2026-04-30&geo=US")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 0
    assert body["from"] == "2026-01-01"
    assert body["to"] == "2026-04-30"
    assert "s.geo_code = %s" in captured["query"]
    assert "US" in captured["params"]
    assert "2026-01-01" in captured["params"]
    assert "2026-04-30" in captured["params"]
