"""Endpoint tests for /v1/orders/* via FastAPI TestClient."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "test-api-key")


def _client():
    from fastapi.testclient import TestClient

    from orchestrator.main import app

    return TestClient(app)


def test_reserve_endpoint_validates_payload() -> None:
    client = _client()
    response = client.post(
        "/v1/orders/reserve",
        json={},
        headers={"X-NETRUN-API-KEY": "test-api-key"},
    )
    assert response.status_code == 422


def test_reserve_endpoint_requires_auth() -> None:
    client = _client()
    response = client.post(
        "/v1/orders/reserve",
        json={"user_id": 1, "sku_id": 1, "quantity": 10},
    )
    assert response.status_code == 401


def test_reserve_endpoint_calls_allocator() -> None:
    from orchestrator.allocator import ReserveResult

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    fake_result = ReserveResult(
        success=True,
        order_ref="ord_endpoint1234",
        expires_at=expires_at,
        proxies_count=10,
    )

    with patch("orchestrator.main._allocator.reserve", new=AsyncMock(return_value=fake_result)):
        client = _client()
        response = client.post(
            "/v1/orders/reserve",
            json={"user_id": 7, "sku_id": 1, "quantity": 10, "reservation_ttl_sec": 300},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["order_ref"] == "ord_endpoint1234"
    assert body["proxies_count"] == 10
    assert body["proxies_url"] == "/v1/orders/ord_endpoint1234/proxies"


def test_reserve_endpoint_insufficient_stock_returns_409() -> None:
    from orchestrator.allocator import ReserveResult

    fake_result = ReserveResult(
        success=False,
        order_ref=None,
        expires_at=None,
        proxies_count=0,
        error="insufficient_stock",
        available_now=42,
    )

    with patch("orchestrator.main._allocator.reserve", new=AsyncMock(return_value=fake_result)):
        client = _client()
        response = client.post(
            "/v1/orders/reserve",
            json={"user_id": 1, "sku_id": 1, "quantity": 1000},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["error"] == "insufficient_stock"
    assert body["available_now"] == 42


def test_release_endpoint_returns_404_when_order_missing() -> None:
    from orchestrator.allocator import ReleaseResult
    from orchestrator.schemas import OrderStatus

    fake_result = ReleaseResult(
        success=False,
        order_ref="ord_unknown",
        status=OrderStatus.RESERVED,
        released_count=0,
        error="order_not_found",
    )

    with patch("orchestrator.main._allocator.release", new=AsyncMock(return_value=fake_result)):
        client = _client()
        response = client.post(
            "/v1/orders/ord_unknown/release",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 404
    assert response.json()["error"] == "order_not_found"


def test_get_order_returns_404_when_missing() -> None:
    with patch("orchestrator.main.asyncio.to_thread", new=AsyncMock(return_value=None)):
        client = _client()
        response = client.get(
            "/v1/orders/ord_missing",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 404
    assert response.json()["error"] == "order_not_found"


# === Wave B-4b: proxies + extend endpoints ===


def test_get_proxies_endpoint_invalid_format() -> None:
    client = _client()
    response = client.get(
        "/v1/orders/ord_test/proxies?format=xml",
        headers={"X-NETRUN-API-KEY": "test-api-key"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_format"


def test_get_proxies_endpoint_returns_text() -> None:
    from orchestrator.allocator import ProxiesResult

    fake_result = ProxiesResult(
        success=True,
        content="socks5://u:p@h:1080",
        content_type="text/plain",
        line_count=1,
    )
    with patch("orchestrator.main._allocator.get_proxies", new=AsyncMock(return_value=fake_result)):
        client = _client()
        response = client.get(
            "/v1/orders/ord_ok/proxies?format=socks5_uri",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-line-count"] == "1"
    assert response.text == "socks5://u:p@h:1080"


def test_get_proxies_endpoint_format_locked() -> None:
    from orchestrator.allocator import ProxiesResult

    fake_result = ProxiesResult(
        success=False,
        error="format_locked",
        locked_format="socks5_uri",
    )
    with patch("orchestrator.main._allocator.get_proxies", new=AsyncMock(return_value=fake_result)):
        client = _client()
        response = client.get(
            "/v1/orders/ord_locked/proxies?format=json",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "format_locked"
    assert body["locked_format"] == "socks5_uri"


# === Wave PROXY-FORMAT.A — template × protocol on the order endpoint ===


def test_get_proxies_templated_socks5_returns_text() -> None:
    from orchestrator.allocator import ProxiesResult

    fake = ProxiesResult(
        success=True,
        content="socks5://u:p:h:1080",
        content_type="text/plain",
        line_count=1,
    )
    with patch(
        "orchestrator.main._allocator.get_proxies_templated",
        new=AsyncMock(return_value=fake),
    ) as mock:
        client = _client()
        response = client.get(
            "/v1/orders/ord_ok/proxies?template=1&protocol=socks5",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 200
    assert response.headers["x-line-count"] == "1"
    assert response.text == "socks5://u:p:h:1080"
    mock.assert_awaited_once_with(order_ref="ord_ok", template=1, protocol="socks5")


def test_get_proxies_templated_https_not_available_returns_409() -> None:
    from orchestrator.allocator import ProxiesResult

    fake = ProxiesResult(success=False, error="https_not_available_for_order")
    with patch(
        "orchestrator.main._allocator.get_proxies_templated",
        new=AsyncMock(return_value=fake),
    ):
        client = _client()
        response = client.get(
            "/v1/orders/ord_socks_only/proxies?template=2&protocol=https",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 409
    assert response.json()["error"] == "https_not_available_for_order"


def test_get_proxies_templated_invalid_template_returns_422() -> None:
    client = _client()
    response = client.get(
        "/v1/orders/ord_x/proxies?template=9&protocol=socks5",
        headers={"X-NETRUN-API-KEY": "test-api-key"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_template"


def test_get_proxies_templated_invalid_protocol_returns_422() -> None:
    client = _client()
    response = client.get(
        "/v1/orders/ord_x/proxies?template=1&protocol=ftp",
        headers={"X-NETRUN-API-KEY": "test-api-key"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "invalid_protocol"


def test_get_proxies_templated_requires_both_params_returns_422() -> None:
    client = _client()
    response = client.get(
        "/v1/orders/ord_x/proxies?template=1",
        headers={"X-NETRUN-API-KEY": "test-api-key"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "template_and_protocol_required"


def test_get_proxies_legacy_format_unaffected_by_template_support() -> None:
    """With neither template nor protocol, the legacy ?format= path runs."""
    from orchestrator.allocator import ProxiesResult

    fake = ProxiesResult(
        success=True,
        content="socks5://u:p@h:1080",
        content_type="text/plain",
        line_count=1,
    )
    with patch(
        "orchestrator.main._allocator.get_proxies",
        new=AsyncMock(return_value=fake),
    ) as mock:
        client = _client()
        response = client.get(
            "/v1/orders/ord_ok/proxies?format=socks5_uri",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 200
    assert response.text == "socks5://u:p@h:1080"
    mock.assert_awaited_once()


def test_extend_endpoint_validates_mutually_exclusive_selectors() -> None:
    client = _client()
    response = client.post(
        "/v1/orders/ord_x/extend",
        json={"duration_days": 30, "inventory_ids": [1, 2], "geo_code": "US"},
        headers={"X-NETRUN-API-KEY": "test-api-key"},
    )
    assert response.status_code == 422


def test_extend_endpoint_calls_allocator() -> None:
    from orchestrator.allocator import ExtendResult

    future = datetime.now(timezone.utc) + timedelta(days=60)
    fake_result = ExtendResult(
        success=True,
        order_ref="ord_ext1",
        extended_count=42,
        new_proxies_expires_at=future,
    )
    with patch("orchestrator.main._allocator.extend_order", new=AsyncMock(return_value=fake_result)):
        client = _client()
        response = client.post(
            "/v1/orders/ord_ext1/extend",
            json={"duration_days": 30},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["order_ref"] == "ord_ext1"
    assert body["extended_count"] == 42


# Cleanup so other tests aren't affected by the env var.
def _cleanup_marker() -> None:
    os.environ.pop("ORCHESTRATOR_API_KEY", None)


# === Wave O-4.A: per-port proxy metadata endpoint ===


def test_get_proxies_meta_returns_per_port_items() -> None:
    """N live ports → one item each with inventory_id/host/port/geo/
    expires_at/status; different per-port expires_at are reflected; NO
    login/password leak."""
    t1 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 8, 15, 9, 30, tzinfo=timezone.utc)
    rows = [
        {"id": 11, "host": "1.2.3.4", "port": 30001, "geo_country": "US",
         "expires_at": t1, "status": "sold",
         "login": "secretlogin", "password": "secretpass"},
        {"id": 12, "host": "1.2.3.4", "port": 30002, "geo_country": "DE",
         "expires_at": t2, "status": "expired_grace",
         "login": "l2", "password": "p2"},
    ]
    with patch(
        "orchestrator.main._allocator.list_order_proxy_meta",
        new=AsyncMock(return_value=rows),
    ):
        client = _client()
        response = client.get(
            "/v1/orders/ord_meta/proxies/meta",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    items = body["items"]
    assert len(items) == 2
    assert items[0]["inventory_id"] == 11
    assert items[0]["host"] == "1.2.3.4"
    assert items[0]["port"] == 30001
    assert items[0]["geo"] == "US"
    assert items[0]["status"] == "sold"
    assert items[0]["expires_at"].startswith("2026-07-01")
    # Per-port срок differs across ports.
    assert items[1]["inventory_id"] == 12
    assert items[1]["status"] == "expired_grace"
    assert items[1]["expires_at"].startswith("2026-08-15")
    # NO credentials in the response — neither key present anywhere.
    raw = response.text
    assert "login" not in raw
    assert "password" not in raw
    assert "secretlogin" not in raw
    assert "secretpass" not in raw


def test_get_proxies_meta_unknown_order_returns_404() -> None:
    with patch(
        "orchestrator.main._allocator.list_order_proxy_meta",
        new=AsyncMock(return_value=None),
    ):
        client = _client()
        response = client.get(
            "/v1/orders/ord_missing/proxies/meta",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 404
    assert response.json()["error"] == "order_not_found"


def test_get_proxies_meta_no_live_ports_returns_empty_items() -> None:
    with patch(
        "orchestrator.main._allocator.list_order_proxy_meta",
        new=AsyncMock(return_value=[]),
    ):
        client = _client()
        response = client.get(
            "/v1/orders/ord_empty/proxies/meta",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["items"] == []


def test_get_proxies_meta_null_geo_and_expiry_pass_through() -> None:
    rows = [
        {"id": 7, "host": "9.9.9.9", "port": 40001, "geo_country": None,
         "expires_at": None, "status": "sold"},
    ]
    with patch(
        "orchestrator.main._allocator.list_order_proxy_meta",
        new=AsyncMock(return_value=rows),
    ):
        client = _client()
        response = client.get(
            "/v1/orders/ord_nulls/proxies/meta",
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["inventory_id"] == 7
    assert item["geo"] is None
    assert item["expires_at"] is None


def test_get_proxies_meta_requires_auth() -> None:
    client = _client()
    response = client.get("/v1/orders/ord_x/proxies/meta")
    assert response.status_code in (401, 403)
