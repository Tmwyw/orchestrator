"""Tests for B-8.3 pergb subsection on /v1/admin/stats."""

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


def _wire_stats_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sales: dict[str, Any] | None = None,
    nodes: dict[str, Any] | None = None,
    inventory: list[dict[str, Any]] | None = None,
    pergb_counts: dict[str, Any] | None = None,
    pergb_bytes: dict[str, Any] | None = None,
    top_skus: list[dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """Install fake fetch_one + fetch_all that route by query substring.

    Returns a dict with the captured query strings — useful for asserting
    that the pergb queries were issued correctly.
    """
    sales = sales or {"orders": 0, "proxies": 0, "revenue": Decimal("0")}
    nodes = nodes or {"ready": 0, "total": 0}
    inventory = inventory or []
    pergb_counts = pergb_counts or {
        "active_accounts": 0,
        "depleted_accounts": 0,
        "expired_accounts": 0,
    }
    pergb_bytes = pergb_bytes or {"bytes_7d": 0}
    top_skus = top_skus if top_skus is not None else []

    seen: dict[str, list[str]] = {"fetch_one": [], "fetch_all": []}

    def fake_fetch_one(query: str, params: Any = None) -> dict[str, Any]:
        seen["fetch_one"].append(query)
        if "from orders" in query and "where status = 'committed'" in query:
            return sales
        if "from nodes" in query:
            return nodes
        if "from traffic_accounts" in query:
            return pergb_counts
        if "from traffic_samples" in query:
            return pergb_bytes
        raise AssertionError(f"unexpected fetch_one query: {query[:80]!r}")

    def fake_fetch_all(query: str, params: Any = None) -> list[dict[str, Any]]:
        seen["fetch_all"].append(query)
        if "from orders" in query and "datacenter_pergb" in query:
            return top_skus
        if "from proxy_inventory" in query:
            return inventory
        raise AssertionError(f"unexpected fetch_all query: {query[:80]!r}")

    monkeypatch.setattr("orchestrator.admin.fetch_one", fake_fetch_one)
    monkeypatch.setattr("orchestrator.admin.fetch_all", fake_fetch_all)
    return seen


def test_stats_includes_pergb_section_with_zero_state(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    """Fresh DB (no pergb activity) — pergb subsection present, all zeros."""
    _wire_stats_fakes(monkeypatch)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/stats")

    assert r.status_code == 200
    body = r.json()
    assert "pergb" in body
    assert body["pergb"] == {
        "active_accounts": 0,
        "depleted_accounts": 0,
        "expired_accounts": 0,
        "bytes_consumed_7d": 0,
        "top_skus_by_revenue_7d": [],
    }


def test_stats_pergb_active_count_matches_db(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    _wire_stats_fakes(
        monkeypatch,
        pergb_counts={
            "active_accounts": 17,
            "depleted_accounts": 3,
            "expired_accounts": 1,
        },
    )

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/stats")

    body = r.json()
    assert body["pergb"]["active_accounts"] == 17
    assert body["pergb"]["depleted_accounts"] == 3
    assert body["pergb"]["expired_accounts"] == 1


def test_stats_pergb_bytes_consumed_7d_uses_sample_deltas(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    """The bytes_consumed_7d query must aggregate sample deltas, not raw
    cumulative readings (counter resets would otherwise inflate)."""
    seen = _wire_stats_fakes(
        monkeypatch,
        pergb_bytes={"bytes_7d": 999_888_777},
    )

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/stats")

    assert r.status_code == 200
    assert r.json()["pergb"]["bytes_consumed_7d"] == 999_888_777
    bytes_query = next(q for q in seen["fetch_one"] if "from traffic_samples" in q)
    assert "bytes_in_delta + bytes_out_delta" in bytes_query
    assert "7 days" in bytes_query


def test_stats_pergb_top_skus_orders_by_revenue_desc(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    seen = _wire_stats_fakes(
        monkeypatch,
        top_skus=[
            {"sku_code": "pergb_us_30gb", "revenue": Decimal("123.40"), "accounts": 8},
            {"sku_code": "pergb_us_5gb", "revenue": Decimal("45.00"), "accounts": 4},
            {"sku_code": "pergb_us_1gb", "revenue": Decimal("9.60"), "accounts": 2},
        ],
    )

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/stats")

    body = r.json()
    items = body["pergb"]["top_skus_by_revenue_7d"]
    assert len(items) == 3
    assert items[0] == {"sku_code": "pergb_us_30gb", "revenue": "123.40", "accounts": 8}
    assert items[2]["sku_code"] == "pergb_us_1gb"
    # The query must filter to pergb-only and order desc + limit 5.
    top_query = next(q for q in seen["fetch_all"] if "datacenter_pergb" in q)
    assert "order by revenue desc" in top_query
    assert "limit 5" in top_query


def test_stats_pergb_top_skus_capped_at_five(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    """The DB query is `LIMIT 5`; verify the handler doesn't widen that
    accidentally by passing through whatever rows it gets."""
    rows = [{"sku_code": f"sku_{i}", "revenue": Decimal(str(10 - i)), "accounts": 1} for i in range(5)]
    _wire_stats_fakes(monkeypatch, top_skus=rows)

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/stats")

    assert r.status_code == 200
    assert len(r.json()["pergb"]["top_skus_by_revenue_7d"]) == 5


def test_stats_pergb_section_decimal_serialized_as_string(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    """Per § 6.10 money convention — Decimal must hit the wire as a string."""
    _wire_stats_fakes(
        monkeypatch,
        top_skus=[{"sku_code": "pergb_us_30gb", "revenue": Decimal("0.00000001"), "accounts": 1}],
    )

    from orchestrator.main import app

    client = TestClient(app)
    r = client.get("/v1/admin/stats")

    rev = r.json()["pergb"]["top_skus_by_revenue_7d"][0]["revenue"]
    assert isinstance(rev, str)
    assert rev == "1E-8" or rev == "0.00000001"
