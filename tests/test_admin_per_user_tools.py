"""Wave PER-USER-TOOLS-1 — tests for the SET-quota + change-expiry
admin endpoints. Uses the same _no_auth fixture pattern as the
existing admin tests; the _sync_* helpers are monkeypatched so the
tests don't need a live Postgres.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


def _client() -> TestClient:
    from orchestrator.main import app

    return TestClient(app)


# ── SET quota ─────────────────────────────────────────────────────


def test_set_quota_happy_path_recomputes_status_active(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    """Quota > used → status='active'."""
    expires = datetime.now(tz=UTC) + timedelta(days=30)

    def fake_sync(order_ref: str, bytes_quota: int) -> dict[str, Any]:
        assert order_ref == "ord_x"
        assert bytes_quota == round(1.5 * (1024**3))
        return {
            "error": "",
            "bytes_quota": bytes_quota,
            "bytes_used": 10_000,
            "status": "active",
            "expires_at": expires,
        }

    monkeypatch.setattr("orchestrator.admin._sync_set_quota", fake_sync)
    r = _client().patch("/v1/admin/orders/ord_x/quota", json={"gb_amount": 1.5})
    assert r.status_code == 200
    body = r.json()
    assert body["order_ref"] == "ord_x"
    assert body["status"] == "active"
    assert body["bytes_quota"] == round(1.5 * (1024**3))
    assert body["bytes_used"] == 10_000
    assert body["bytes_remaining"] == body["bytes_quota"] - 10_000


def test_set_quota_below_used_marks_depleted(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    expires = datetime.now(tz=UTC) + timedelta(days=30)

    def fake_sync(order_ref: str, bytes_quota: int) -> dict[str, Any]:
        return {
            "error": "",
            "bytes_quota": bytes_quota,
            "bytes_used": bytes_quota + 1_000,  # over-quota
            "status": "depleted",
            "expires_at": expires,
        }

    monkeypatch.setattr("orchestrator.admin._sync_set_quota", fake_sync)
    r = _client().patch("/v1/admin/orders/ord_x/quota", json={"gb_amount": 0.5})
    assert r.status_code == 200
    assert r.json()["status"] == "depleted"
    assert r.json()["bytes_remaining"] == 0  # clamped


def test_set_quota_unknown_order_returns_404(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    monkeypatch.setattr(
        "orchestrator.admin._sync_set_quota",
        lambda *_a, **_k: {"error": "not_found"},
    )
    r = _client().patch("/v1/admin/orders/ord_y/quota", json={"gb_amount": 1.0})
    assert r.status_code == 404


def test_set_quota_archived_account_returns_409(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    """We don't resurrect archived/expired accounts via a quota
    update — return 409 so the caller surfaces a precise banner."""
    monkeypatch.setattr(
        "orchestrator.admin._sync_set_quota",
        lambda *_a, **_k: {"error": "closed", "status": "archived"},
    )
    r = _client().patch("/v1/admin/orders/ord_x/quota", json={"gb_amount": 1.0})
    assert r.status_code == 409


def test_set_quota_validates_positive_gb(_no_auth: None) -> None:
    r = _client().patch("/v1/admin/orders/ord_x/quota", json={"gb_amount": 0})
    assert r.status_code == 422


# ── Change expiry ────────────────────────────────────────────────


def test_change_expiry_add_happy_path(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    old = datetime(2026, 5, 1, tzinfo=UTC)
    new = datetime(2026, 5, 31, tzinfo=UTC)

    def fake_sync(order_ref: str, mode: str, days: int) -> dict[str, Any]:
        assert order_ref == "ord_x"
        assert mode == "add"
        assert days == 30
        return {
            "error": "",
            "old_expires_at": old,
            "new_expires_at": new,
            "affected_inventory_count": 5,
        }

    monkeypatch.setattr("orchestrator.admin._sync_change_expiry", fake_sync)
    r = _client().patch(
        "/v1/admin/orders/ord_x/expiry", json={"mode": "add", "days": 30}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "add"
    assert body["days"] == 30
    assert body["affected_inventory_count"] == 5


def test_change_expiry_set_replaces_unconditionally(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    new = datetime.now(tz=UTC) + timedelta(days=10)

    def fake_sync(order_ref: str, mode: str, days: int) -> dict[str, Any]:
        assert mode == "set"
        return {
            "error": "",
            "old_expires_at": None,
            "new_expires_at": new,
            "affected_inventory_count": 0,
        }

    monkeypatch.setattr("orchestrator.admin._sync_change_expiry", fake_sync)
    r = _client().patch(
        "/v1/admin/orders/ord_x/expiry", json={"mode": "set", "days": 10}
    )
    assert r.status_code == 200


def test_change_expiry_subtract_into_past_returns_422(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    monkeypatch.setattr(
        "orchestrator.admin._sync_change_expiry",
        lambda *_a, **_k: {"error": "past"},
    )
    r = _client().patch(
        "/v1/admin/orders/ord_x/expiry", json={"mode": "subtract", "days": 5000}
    )
    assert r.status_code == 422


def test_change_expiry_subtract_null_base_returns_409(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    monkeypatch.setattr(
        "orchestrator.admin._sync_change_expiry",
        lambda *_a, **_k: {"error": "null_base"},
    )
    r = _client().patch(
        "/v1/admin/orders/ord_x/expiry", json={"mode": "subtract", "days": 5}
    )
    assert r.status_code == 409


def test_change_expiry_unknown_order_returns_404(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    monkeypatch.setattr(
        "orchestrator.admin._sync_change_expiry",
        lambda *_a, **_k: {"error": "not_found"},
    )
    r = _client().patch(
        "/v1/admin/orders/ord_y/expiry", json={"mode": "add", "days": 1}
    )
    assert r.status_code == 404


def test_change_expiry_validates_mode_pattern(_no_auth: None) -> None:
    r = _client().patch(
        "/v1/admin/orders/ord_x/expiry", json={"mode": "rewind", "days": 1}
    )
    assert r.status_code == 422


def test_change_expiry_validates_days_range(_no_auth: None) -> None:
    r = _client().patch(
        "/v1/admin/orders/ord_x/expiry", json={"mode": "add", "days": 9999}
    )
    assert r.status_code == 422
    r = _client().patch(
        "/v1/admin/orders/ord_x/expiry", json={"mode": "add", "days": 0}
    )
    assert r.status_code == 422
