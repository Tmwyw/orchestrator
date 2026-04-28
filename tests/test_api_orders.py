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


# Cleanup so other tests aren't affected by the env var.
def _cleanup_marker() -> None:
    os.environ.pop("ORCHESTRATOR_API_KEY", None)
