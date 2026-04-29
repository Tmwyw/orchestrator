"""Endpoint tests for POST /v1/nodes/enroll (Wave B-6.2)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "test-api-key")


def _client():
    from fastapi.testclient import TestClient

    from orchestrator.main import app

    return TestClient(app)


def _make_fake_connect(
    *,
    upsert_row: dict[str, Any] | None = None,
    bound_codes: list[str] | None = None,
):
    """Return a fake `connect()` context manager that records executes."""
    upsert_row = upsert_row or {"id": "fake-id", "name": "fake-name"}
    bound_codes = bound_codes or []

    fetchall_queue: list[list[dict[str, Any]]] = []
    fetchone_queue: list[dict[str, Any] | None] = []

    fetchone_queue.append(upsert_row)
    fetchall_queue.append([{"code": c} for c in bound_codes])

    cursor = MagicMock()
    cursor.execute = MagicMock()
    cursor.fetchone = MagicMock(side_effect=lambda: fetchone_queue.pop(0) if fetchone_queue else None)
    cursor.fetchall = MagicMock(side_effect=lambda: fetchall_queue.pop(0) if fetchall_queue else [])
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=None)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)

    @contextmanager
    def fake_connect():
        yield conn

    return fake_connect, cursor


def test_enroll_describe_unreachable_returns_502() -> None:
    with patch("orchestrator.main.node_client.describe", side_effect=RuntimeError("boom")):
        client = _client()
        response = client.post(
            "/v1/nodes/enroll",
            json={"agent_url": "http://10.0.0.5:8085"},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 502
    body = response.json()
    assert body["error"] == "describe_unreachable"
    assert "boom" in (body.get("detail") or "")


def test_enroll_api_key_required_but_not_provided() -> None:
    describe_payload = {"api_key_required": True, "geo_code": "US", "capacity": 1000}
    with patch("orchestrator.main.node_client.describe", return_value=describe_payload):
        client = _client()
        response = client.post(
            "/v1/nodes/enroll",
            json={"agent_url": "http://10.0.0.5:8085"},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 400
    assert response.json()["error"] == "api_key_required_by_node"


def test_enroll_health_not_ready_without_force() -> None:
    describe_payload = {"api_key_required": False, "geo_code": "US", "capacity": 1000}
    not_ready_health = {"success": False, "status": "failed", "ipv6": {"ok": False}}
    with (
        patch("orchestrator.main.node_client.describe", return_value=describe_payload),
        patch("orchestrator.main.check_health", return_value=not_ready_health),
    ):
        client = _client()
        response = client.post(
            "/v1/nodes/enroll",
            json={"agent_url": "http://10.0.0.5:8085"},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "node_health_not_ready"
    assert "diagnostics" in (body.get("extra") or {})


def test_enroll_health_not_ready_with_force_saves_unavailable() -> None:
    describe_payload = {"api_key_required": False, "geo_code": "US", "capacity": 2000}
    not_ready_health = {"success": False, "status": "failed", "ipv6": {"ok": False}}
    fake_connect, _ = _make_fake_connect()
    with (
        patch("orchestrator.main.node_client.describe", return_value=describe_payload),
        patch("orchestrator.main.check_health", return_value=not_ready_health),
        patch("orchestrator.main.connect", new=fake_connect),
    ):
        client = _client()
        response = client.post(
            "/v1/nodes/enroll",
            json={"agent_url": "http://10.0.0.5:8085", "force": True},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["node"]["status"] == "unavailable"
    assert body["node"]["geo"] == "US"


def test_enroll_success_with_describe_geo() -> None:
    describe_payload = {
        "api_key_required": False,
        "geo_code": "US",
        "capacity": 1500,
        "max_parallel_jobs": 1,
        "max_batch_size": 1500,
        "generator_script": "/opt/netrun/node_runtime/generator/proxyyy_automated.sh",
    }
    ready_health = {"success": True, "status": "ready", "ipv6": {"ok": True}, "ipv6Egress": {"ok": True}}
    fake_connect, _ = _make_fake_connect()
    with (
        patch("orchestrator.main.node_client.describe", return_value=describe_payload),
        patch("orchestrator.main.check_health", return_value=ready_health),
        patch("orchestrator.main.connect", new=fake_connect),
    ):
        client = _client()
        response = client.post(
            "/v1/nodes/enroll",
            json={"agent_url": "http://10.0.0.5:8085"},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["node"]["geo"] == "US"
    assert body["node"]["status"] == "ready"
    assert body["node"]["capacity"] == 1500
    assert body["describe_geo_code"] == "US"


def test_enroll_success_with_payload_geo_override() -> None:
    describe_payload = {"api_key_required": False, "geo_code": None, "capacity": 1000}
    ready_health = {"success": True, "status": "ready", "ipv6": {"ok": True}, "ipv6Egress": {"ok": True}}
    fake_connect, _ = _make_fake_connect()
    with (
        patch("orchestrator.main.node_client.describe", return_value=describe_payload),
        patch("orchestrator.main.check_health", return_value=ready_health),
        patch("orchestrator.main.connect", new=fake_connect),
    ):
        client = _client()
        response = client.post(
            "/v1/nodes/enroll",
            json={"agent_url": "http://10.0.0.5:8085", "geo_code": "DE"},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["node"]["geo"] == "DE"
    assert body["describe_geo_code"] is None


def test_enroll_auto_bind_emits_bound_skus() -> None:
    describe_payload = {"api_key_required": False, "geo_code": "US", "capacity": 1500}
    ready_health = {"success": True, "status": "ready", "ipv6": {"ok": True}, "ipv6Egress": {"ok": True}}
    fake_connect, _ = _make_fake_connect(bound_codes=["ipv6_us_socks5", "ipv6_us_http"])
    with (
        patch("orchestrator.main.node_client.describe", return_value=describe_payload),
        patch("orchestrator.main.check_health", return_value=ready_health),
        patch("orchestrator.main.connect", new=fake_connect),
    ):
        client = _client()
        response = client.post(
            "/v1/nodes/enroll",
            json={"agent_url": "http://10.0.0.5:8085", "auto_bind_active_skus": True},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["auto_bound_skus"] == ["ipv6_us_socks5", "ipv6_us_http"]


def test_enroll_requires_orchestrator_api_key() -> None:
    client = _client()
    response = client.post(
        "/v1/nodes/enroll",
        json={"agent_url": "http://10.0.0.5:8085"},
    )
    assert response.status_code == 401


def test_enroll_existing_url_updates_in_place() -> None:
    """ON CONFLICT (url) preserves the existing node id (Wave B-6.3 hotfix).

    Simulates a row that was originally inserted by the legacy add_node.sh path
    with a random UUID. The enroll deterministic id (uuid5 of the URL) does not
    match, but the UPSERT collides on the url unique key and RETURNING surfaces
    the row's actual id. Two consecutive enrolls for the same URL must return
    the same id.
    """
    describe_payload = {
        "api_key_required": False,
        "geo_code": "US",
        "capacity": 1500,
        "max_parallel_jobs": 1,
        "max_batch_size": 1500,
        "generator_script": "/opt/netrun/node_runtime/generator/proxyyy_automated.sh",
    }
    ready_health = {"success": True, "status": "ready", "ipv6": {"ok": True}, "ipv6Egress": {"ok": True}}

    legacy_existing_id = "legacy-random-uuid-from-add_node-sh"
    fake_connect, _ = _make_fake_connect(
        upsert_row={"id": legacy_existing_id, "name": "node-de-1"},
    )
    with (
        patch("orchestrator.main.node_client.describe", return_value=describe_payload),
        patch("orchestrator.main.check_health", return_value=ready_health),
        patch("orchestrator.main.connect", new=fake_connect),
    ):
        client = _client()
        response_1 = client.post(
            "/v1/nodes/enroll",
            json={"agent_url": "http://10.0.0.5:8085"},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    fake_connect_2, _ = _make_fake_connect(
        upsert_row={"id": legacy_existing_id, "name": "node-de-1"},
    )
    with (
        patch("orchestrator.main.node_client.describe", return_value=describe_payload),
        patch("orchestrator.main.check_health", return_value=ready_health),
        patch("orchestrator.main.connect", new=fake_connect_2),
    ):
        response_2 = client.post(
            "/v1/nodes/enroll",
            json={"agent_url": "http://10.0.0.5:8085"},
            headers={"X-NETRUN-API-KEY": "test-api-key"},
        )

    assert response_1.status_code == 200
    assert response_2.status_code == 200
    assert response_1.json()["node"]["id"] == legacy_existing_id
    assert response_2.json()["node"]["id"] == legacy_existing_id
    assert response_1.json()["node"]["name"] == "node-de-1"
