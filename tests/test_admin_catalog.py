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


def test_list_skus_returns_items_and_total(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
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


def test_list_skus_applies_filters(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
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


def test_get_sku_returns_detail_with_breakdown(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
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


def test_get_sku_returns_404_when_missing(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
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


# === POST /v1/admin/skus ===


def test_create_sku_happy_path(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    inserted_row = {
        "id": 42,
        "code": "ipv6_de_socks5",
        "product_kind": "ipv6",
        "geo_code": "DE",
        "protocol": "socks5",
        "duration_days": 30,
        "price_per_piece": Decimal("2.40"),
        "price_per_gb": None,
        "target_stock": 5000,
        "refill_batch_size": 500,
        "validation_require_ipv6": True,
        "is_active": True,
        "metadata": {},
        "created_at": datetime(2026, 5, 18, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 18, tzinfo=timezone.utc),
    }

    def fake_create_sku_sync(payload: Any) -> dict[str, Any]:
        assert payload.code == "ipv6_de_socks5"
        assert payload.price_per_piece == Decimal("2.40")
        return inserted_row

    monkeypatch.setattr("orchestrator.admin_catalog._create_sku_sync", fake_create_sku_sync)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/admin/skus",
        json={
            "code": "ipv6_de_socks5",
            "product_kind": "ipv6",
            "geo_code": "DE",
            "protocol": "socks5",
            "duration_days": 30,
            "price_per_piece": "2.40",
            "target_stock": 5000,
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["id"] == 42
    assert body["code"] == "ipv6_de_socks5"
    assert body["stock_total"]["available"] == 0
    assert body["stock_breakdown"] == []


def test_create_sku_409_on_duplicate_code(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr("orchestrator.admin_catalog._create_sku_sync", lambda _p: "duplicate_code")
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/admin/skus",
        json={
            "code": "ipv6_de_socks5",
            "product_kind": "ipv6",
            "geo_code": "DE",
            "protocol": "socks5",
            "price_per_piece": "2.40",
            "target_stock": 5000,
        },
    )
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_code"


def test_create_sku_409_on_duplicate_kind_geo_protocol(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._create_sku_sync",
        lambda _p: "duplicate_kind_geo_protocol",
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/admin/skus",
        json={
            "code": "ipv6_de_socks5_v2",
            "product_kind": "ipv6",
            "geo_code": "DE",
            "protocol": "socks5",
            "price_per_piece": "2.40",
            "target_stock": 5000,
        },
    )
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_kind_geo_protocol"


def test_create_sku_400_on_invalid_price_too_high(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    # Should not even reach the service — Pydantic rejects price > 10000.
    monkeypatch.setattr(
        "orchestrator.admin_catalog._create_sku_sync",
        lambda _p: (_ for _ in ()).throw(AssertionError("service must not be called")),
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/admin/skus",
        json={
            "code": "ipv6_xx_socks5",
            "product_kind": "ipv6",
            "geo_code": "XX",
            "protocol": "socks5",
            "price_per_piece": "99999.99",
            "target_stock": 100,
        },
    )
    assert r.status_code == 422  # FastAPI validation error


def test_create_sku_400_on_invalid_target_stock_zero(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._create_sku_sync",
        lambda _p: (_ for _ in ()).throw(AssertionError("service must not be called")),
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/admin/skus",
        json={
            "code": "ipv6_xx_socks5",
            "product_kind": "ipv6",
            "geo_code": "XX",
            "protocol": "socks5",
            "price_per_piece": "2.50",
            "target_stock": 0,
        },
    )
    assert r.status_code == 422


def test_create_sku_400_when_pergb_missing_price_per_gb(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._create_sku_sync",
        lambda _p: (_ for _ in ()).throw(AssertionError("service must not be called")),
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/admin/skus",
        json={
            "code": "pergb_global",
            "product_kind": "datacenter_pergb",
            "geo_code": "",
            "protocol": "socks5",
            "target_stock": 1000,
        },
    )
    assert r.status_code == 422


# === PATCH /v1/admin/skus/{id} ===


def _stub_updated_row(price: str = "2.80", target_stock: int = 6000) -> dict[str, Any]:
    return {
        "id": 1,
        "code": "ipv6_us_socks5",
        "product_kind": "ipv6",
        "geo_code": "US",
        "protocol": "socks5",
        "duration_days": 30,
        "price_per_piece": Decimal(price),
        "price_per_gb": None,
        "target_stock": target_stock,
        "refill_batch_size": 500,
        "validation_require_ipv6": True,
        "is_active": True,
        "metadata": {},
        "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 18, tzinfo=timezone.utc),
    }


def test_patch_sku_happy_path(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    new_row = _stub_updated_row(price="3.00", target_stock=8000)
    captured: dict[str, Any] = {}

    def fake_update(sku_id: int, payload: Any) -> dict[str, Any]:
        captured["sku_id"] = sku_id
        captured["fields"] = payload.model_dump(exclude_none=True)
        return new_row

    monkeypatch.setattr("orchestrator.admin_catalog._update_sku_sync", fake_update)
    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", lambda *_a, **_kw: [])

    from orchestrator.main import app

    client = TestClient(app)
    r = client.patch(
        "/v1/admin/skus/1",
        json={"price_per_piece": "3.00", "target_stock": 8000},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 1
    assert body["price_per_piece"] == "3.00"
    assert body["target_stock"] == 8000
    assert captured["sku_id"] == 1
    assert captured["fields"]["target_stock"] == 8000


def test_patch_sku_404_when_missing(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._update_sku_sync",
        lambda _id, _p: "sku_not_found",
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.patch("/v1/admin/skus/9999", json={"price_per_piece": "3.00"})
    assert r.status_code == 404
    assert r.json()["error"] == "sku_not_found"


def test_patch_sku_400_when_no_fields(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._update_sku_sync",
        lambda _id, _p: "no_fields_to_update",
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.patch("/v1/admin/skus/1", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "no_fields_to_update"


# === DELETE /v1/admin/skus/{id} ===


def test_delete_sku_happy(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._delete_sku_sync",
        lambda _id: {"id": 1, "is_active": False, "updated_at": "now"},
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.delete("/v1/admin/skus/1")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["deleted_id"] == 1


def test_delete_sku_blocked_by_pending_orders(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr("orchestrator.admin_catalog._delete_sku_sync", lambda _id: "pending_orders")
    from orchestrator.main import app

    client = TestClient(app)
    r = client.delete("/v1/admin/skus/1")
    assert r.status_code == 409
    assert r.json()["error"] == "pending_orders"


def test_delete_sku_404_when_missing(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr("orchestrator.admin_catalog._delete_sku_sync", lambda _id: "sku_not_found")
    from orchestrator.main import app

    client = TestClient(app)
    r = client.delete("/v1/admin/skus/9999")
    assert r.status_code == 404
    assert r.json()["error"] == "sku_not_found"


# === Unit test for _jsonify_diff ===


def test_jsonify_diff_coerces_decimals_to_strings() -> None:
    from orchestrator.admin_catalog import _jsonify_diff

    diff: dict[str, dict[str, Any]] = {
        "price_per_piece": {"old": Decimal("2.50"), "new": Decimal("3.00")},
        "target_stock": {"old": 5000, "new": 8000},
        "is_active": {"old": True, "new": False},
    }
    result = _jsonify_diff(diff)
    assert result["price_per_piece"] == {"old": "2.50", "new": "3.00"}
    assert result["target_stock"] == {"old": 5000, "new": 8000}
    assert result["is_active"] == {"old": True, "new": False}


# === GET /v1/admin/skus/{id}/bindings ===


def _stub_binding(node_id: str = "node-us-1", geo: str = "US") -> dict[str, Any]:
    return {
        "node_id": node_id,
        "node_name": node_id,
        "node_geo": geo,
        "weight": 100,
        "max_batch_size": 1500,
        "is_active": True,
        "created_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
    }


def test_list_bindings_happy(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    rows = [_stub_binding("node-us-1"), _stub_binding("node-us-2")]
    monkeypatch.setattr("orchestrator.admin_catalog._list_bindings_sync", lambda _id: rows)
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus/1/bindings")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["items"][0]["node_id"] == "node-us-1"


def test_list_bindings_404_sku_missing(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr("orchestrator.admin_catalog._list_bindings_sync", lambda _id: "sku_not_found")
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus/9999/bindings")
    assert r.status_code == 404


# === POST /v1/admin/skus/{id}/bindings ===


def test_add_binding_happy(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._add_binding_sync",
        lambda _sku, _p: _stub_binding("node-us-3"),
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/admin/skus/1/bindings",
        json={"node_id": "node-us-3", "weight": 100, "max_batch_size": 1500},
    )
    assert r.status_code == 201
    assert r.json()["node_id"] == "node-us-3"


@pytest.mark.parametrize(
    "code,status",
    [
        ("sku_not_found", 404),
        ("node_not_found", 404),
        ("geo_mismatch", 409),
        ("binding_exists", 409),
    ],
)
def test_add_binding_error_codes(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None, code: str, status: int
) -> None:
    monkeypatch.setattr("orchestrator.admin_catalog._add_binding_sync", lambda _sku, _p: code)
    from orchestrator.main import app

    client = TestClient(app)
    r = client.post(
        "/v1/admin/skus/1/bindings",
        json={"node_id": "node-us-3"},
    )
    assert r.status_code == status
    assert r.json()["error"] == code


# === PATCH /v1/admin/skus/{id}/bindings/{node_id} ===


def test_patch_binding_happy(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    updated = _stub_binding()
    updated["weight"] = 200
    monkeypatch.setattr(
        "orchestrator.admin_catalog._update_binding_sync",
        lambda _sku, _node, _p: updated,
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.patch("/v1/admin/skus/1/bindings/node-us-1", json={"weight": 200})
    assert r.status_code == 200
    assert r.json()["weight"] == 200


def test_patch_binding_400_no_fields(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._update_binding_sync",
        lambda _sku, _node, _p: "no_fields_to_update",
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.patch("/v1/admin/skus/1/bindings/node-us-1", json={})
    assert r.status_code == 400


def test_patch_binding_404(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._update_binding_sync",
        lambda _sku, _node, _p: "binding_not_found",
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.patch("/v1/admin/skus/1/bindings/node-missing", json={"weight": 50})
    assert r.status_code == 404


# === DELETE /v1/admin/skus/{id}/bindings/{node_id} ===


def test_delete_binding_happy(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._delete_binding_sync",
        lambda _sku, _node: {"sku_id": 1, "node_id": "node-us-1"},
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.delete("/v1/admin/skus/1/bindings/node-us-1")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["sku_id"] == 1
    assert body["node_id"] == "node-us-1"


def test_delete_binding_404(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._delete_binding_sync",
        lambda _sku, _node: "binding_not_found",
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.delete("/v1/admin/skus/1/bindings/node-missing")
    assert r.status_code == 404


# === GET /v1/admin/skus/{id}/tiers ===


def test_list_tiers_happy(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    rows = [
        {"gb": 10, "price_per_gb": Decimal("1.00")},
        {"gb": 50, "price_per_gb": Decimal("0.80")},
        {"gb": 200, "price_per_gb": Decimal("0.50")},
    ]
    monkeypatch.setattr("orchestrator.admin_catalog._list_tiers_sync", lambda _id: rows)
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus/1/tiers")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 3
    assert body["items"][0]["gb"] == 10
    assert body["items"][0]["price_per_gb"] == "1.00"
    assert body["items"][2]["gb"] == 200


def test_list_tiers_404_sku_missing(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr("orchestrator.admin_catalog._list_tiers_sync", lambda _id: "sku_not_found")
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus/9999/tiers")
    assert r.status_code == 404


def test_list_tiers_empty_returns_empty_list(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr("orchestrator.admin_catalog._list_tiers_sync", lambda _id: [])
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/skus/1/tiers")
    assert r.status_code == 200
    assert r.json() == {"items": []}


# === PUT /v1/admin/skus/{id}/tiers ===


def test_put_tiers_happy(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    new_rows = [
        {"gb": 10, "price_per_gb": Decimal("1.00")},
        {"gb": 100, "price_per_gb": Decimal("0.70")},
    ]
    captured: dict[str, Any] = {}

    def fake_replace(sku_id: int, payload: Any) -> list[dict[str, Any]]:
        captured["sku_id"] = sku_id
        captured["tiers"] = [(t.gb, str(t.price_per_gb)) for t in payload.tiers]
        return new_rows

    monkeypatch.setattr("orchestrator.admin_catalog._replace_tiers_sync", fake_replace)
    from orchestrator.main import app

    client = TestClient(app)
    r = client.put(
        "/v1/admin/skus/1/tiers",
        json={
            "tiers": [
                {"gb": 10, "price_per_gb": "1.00"},
                {"gb": 100, "price_per_gb": "0.70"},
            ]
        },
    )
    assert r.status_code == 200
    assert len(r.json()["items"]) == 2
    assert captured["sku_id"] == 1
    assert captured["tiers"] == [(10, "1.00"), (100, "0.70")]


def test_put_tiers_422_when_gb_not_ascending(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._replace_tiers_sync",
        lambda _id, _p: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.put(
        "/v1/admin/skus/1/tiers",
        json={
            "tiers": [
                {"gb": 100, "price_per_gb": "0.50"},
                {"gb": 50, "price_per_gb": "0.80"},
            ]
        },
    )
    assert r.status_code == 422


def test_put_tiers_422_when_price_not_monotonic(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._replace_tiers_sync",
        lambda _id, _p: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.put(
        "/v1/admin/skus/1/tiers",
        json={
            "tiers": [
                {"gb": 10, "price_per_gb": "0.50"},
                {"gb": 100, "price_per_gb": "1.00"},  # higher price at higher gb
            ]
        },
    )
    assert r.status_code == 422


def test_put_tiers_404_when_sku_missing(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._replace_tiers_sync",
        lambda _id, _p: "sku_not_found",
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.put(
        "/v1/admin/skus/9999/tiers",
        json={"tiers": [{"gb": 10, "price_per_gb": "1.00"}]},
    )
    assert r.status_code == 404


def test_put_tiers_400_when_sku_not_pergb(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.admin_catalog._replace_tiers_sync",
        lambda _id, _p: "sku_not_pergb",
    )
    from orchestrator.main import app

    client = TestClient(app)
    r = client.put(
        "/v1/admin/skus/1/tiers",
        json={"tiers": [{"gb": 10, "price_per_gb": "1.00"}]},
    )
    assert r.status_code == 400
    assert r.json()["error"] == "sku_not_pergb"


# === GET /v1/admin/geos ===


def test_list_geos_populated(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    rows = [
        {"geo_code": "DE", "sku_count": 2},
        {"geo_code": "UK", "sku_count": 1},
        {"geo_code": "US", "sku_count": 4},
    ]

    def fake_fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
        assert "DISTINCT" not in query.upper() or "GROUP BY geo_code" in query
        assert "geo_code <> ''" in query
        return rows

    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", fake_fetch_all)
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/geos")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 3
    assert body["items"][0] == {"geo_code": "DE", "sku_count": 2}


def test_list_geos_empty(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", lambda *_a, **_kw: [])
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/geos")
    assert r.status_code == 200
    assert r.json() == {"items": []}


# === GET /v1/admin/product_kinds ===


def test_list_product_kinds_returns_hardcoded_with_counts(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    rows = [
        {"product_kind": "ipv6", "sku_count": 7},
        {"product_kind": "datacenter_pergb", "sku_count": 1},
    ]
    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", lambda *_a, **_kw: rows)
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/product_kinds")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    by_kind = {it["kind"]: it for it in body["items"]}
    assert by_kind["ipv6"]["name"] == "IPv6 SOCKS5"
    assert by_kind["ipv6"]["sku_count"] == 7
    assert by_kind["datacenter_pergb"]["name"] == "Pay-per-GB Datacenter"
    assert by_kind["datacenter_pergb"]["sku_count"] == 1


def test_list_product_kinds_zero_counts_when_no_skus(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr("orchestrator.admin_catalog.fetch_all", lambda *_a, **_kw: [])
    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/product_kinds")
    assert r.status_code == 200
    body = r.json()
    assert all(it["sku_count"] == 0 for it in body["items"])
    assert {it["kind"] for it in body["items"]} == {"ipv6", "datacenter_pergb"}
