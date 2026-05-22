"""Tests for the live-ping ``GET /v1/nodes/health`` endpoint.

Wave HEALTH-FIX-1 Phase A. Coverage:

- Mock ``node_client.check_health`` so a subset of nodes is reachable
  and the rest time out → assert per-node JSON flags, latencies, and
  the ``last_check`` ISO timestamp are populated correctly.
- Parallelism: the fan-out runs through ``asyncio.gather`` so total
  wall-clock latency should be approximately ``max(per-node latency)``
  rather than the sum. A 3-node fixture with a 0.5 s sleep inside the
  blocking mock proves this within a comfortable budget.
- Side effect: every reachable node bumps ``last_heartbeat_at`` via a
  single bulk ``UPDATE``. The test captures the parameters passed to
  ``cursor.execute`` and asserts the id list matches reachable nodes
  only.
- Auth: missing ``X-NETRUN-API-KEY`` returns 401.
- Empty fixture (no nodes) → ``{"success": true, "items": []}`` with
  no DB write and no ping fanout.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
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


def _make_nodes() -> list[dict[str, Any]]:
    return [
        {
            "id": "node-up",
            "name": "Tokyo",
            "geo": "JP",
            "url": "http://node-up.example",
            "api_key": "k1",
            "runtime_status": "active",
        },
        {
            "id": "node-down",
            "name": "Frankfurt",
            "geo": "DE",
            "url": "http://node-down.example",
            "api_key": "k2",
            "runtime_status": "offline",
        },
        {
            "id": "node-degraded",
            "name": "Dallas",
            "geo": "US",
            "url": "http://node-degraded.example",
            "api_key": "k3",
            "runtime_status": "degraded",
        },
    ]


def _ready_payload() -> dict[str, Any]:
    """Minimum shape that satisfies ``node_health_ready``."""
    return {
        "success": True,
        "status": "ready",
        "ipv6": {"ok": True},
    }


class _MockCursor:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def __enter__(self) -> _MockCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, query: str, params: Any = None) -> None:
        self._store.setdefault("calls", []).append((query, params))


class _MockConn:
    def __init__(self, store: dict[str, Any]) -> None:
        self._store = store

    def __enter__(self) -> _MockConn:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def cursor(self) -> _MockCursor:
        return _MockCursor(self._store)


def _patch_connect(monkeypatch: pytest.MonkeyPatch, store: dict[str, Any]) -> None:
    """Install a ``connect()`` stub that records every ``execute`` call
    into ``store["calls"]``."""

    @contextmanager
    def fake_connect():  # type: ignore[no-untyped-def]
        yield _MockConn(store)

    monkeypatch.setattr("orchestrator.main.connect", fake_connect)


# ── happy path: mixed reachable + unreachable + ready_payload mismatch ─


def test_nodes_health_endpoint_mixed_reachability(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    nodes = _make_nodes()
    monkeypatch.setattr("orchestrator.main.fetch_all", lambda *a, **k: nodes)

    def fake_check_health(url: str, api_key: str | None, timeout_sec: int) -> dict[str, Any]:
        if url == "http://node-up.example":
            return _ready_payload()
        if url == "http://node-degraded.example":
            # Reachable but not "ready" — ipv6 not OK. Counts as unreachable.
            return {"success": True, "status": "ready", "ipv6": {"ok": False}}
        # node-down — connection refused.
        raise RuntimeError("connection refused")

    monkeypatch.setattr("orchestrator.main.check_health", fake_check_health)

    db_calls: dict[str, Any] = {}
    _patch_connect(monkeypatch, db_calls)

    from orchestrator.main import app

    client = TestClient(app)
    response = client.get("/v1/nodes/health", headers={"X-NETRUN-API-KEY": "test-api-key"})
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["items"]) == 3

    by_id = {item["id"]: item for item in body["items"]}
    up = by_id["node-up"]
    down = by_id["node-down"]
    degraded = by_id["node-degraded"]

    assert up["reachable"] is True
    assert up["latency_ms"] is not None
    assert up["latency_ms"] >= 0
    assert up["runtime_status"] == "active"
    assert up["geo"] == "JP"
    assert up["name"] == "Tokyo"
    assert "last_check" in up
    assert up["last_check"].endswith("+00:00") or up["last_check"].endswith("Z")

    assert down["reachable"] is False
    assert down["latency_ms"] is None
    assert down["runtime_status"] == "offline"

    # Reachable HTTP but ipv6 not ready → reachable=False from
    # node_health_ready(); latency still recorded because the call
    # itself completed.
    assert degraded["reachable"] is False
    assert degraded["latency_ms"] is not None
    assert degraded["runtime_status"] == "degraded"

    # last_heartbeat_at updated only for the one truly reachable node.
    updates = [params for query, params in db_calls.get("calls", []) if "last_heartbeat_at" in query]
    assert len(updates) == 1
    (id_list,) = updates[0]
    assert id_list == ["node-up"]


# ── empty fixture: zero nodes — no fanout, no DB write ──────────────


def test_nodes_health_endpoint_empty_node_list(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr("orchestrator.main.fetch_all", lambda *a, **k: [])

    fanout_calls: list[str] = []

    def fake_check_health(*a: Any, **k: Any) -> dict[str, Any]:
        fanout_calls.append("called")
        return _ready_payload()

    monkeypatch.setattr("orchestrator.main.check_health", fake_check_health)

    db_calls: dict[str, Any] = {}
    _patch_connect(monkeypatch, db_calls)

    from orchestrator.main import app

    client = TestClient(app)
    response = client.get("/v1/nodes/health", headers={"X-NETRUN-API-KEY": "test-api-key"})
    assert response.status_code == 200
    body = response.json()
    assert body == {"success": True, "items": []}

    # No ping fired, no UPDATE attempted.
    assert fanout_calls == []
    assert db_calls.get("calls", []) == []


# ── parallelism: 3 nodes × 0.5s ≈ 0.5s wall, not 1.5s ───────────────


def test_nodes_health_endpoint_pings_in_parallel(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    nodes = _make_nodes()
    monkeypatch.setattr("orchestrator.main.fetch_all", lambda *a, **k: nodes)

    sleep_seconds = 0.5

    def slow_check_health(url: str, api_key: str | None, timeout_sec: int) -> dict[str, Any]:
        # Simulate a node that takes 0.5 s to respond.
        time.sleep(sleep_seconds)
        return _ready_payload()

    monkeypatch.setattr("orchestrator.main.check_health", slow_check_health)

    db_calls: dict[str, Any] = {}
    _patch_connect(monkeypatch, db_calls)

    from orchestrator.main import app

    client = TestClient(app)
    wall_start = time.perf_counter()
    response = client.get("/v1/nodes/health", headers={"X-NETRUN-API-KEY": "test-api-key"})
    wall = time.perf_counter() - wall_start

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 3
    assert all(item["reachable"] for item in body["items"])

    # Sequential would be ~1.5 s; parallel should finish well under 1 s.
    # Give a generous ceiling to absorb CI jitter — anything below
    # ~3 * sleep_seconds proves we are not serializing.
    assert wall < sleep_seconds * 2, (
        f"endpoint did not run in parallel: wall={wall:.3f}s, per-node sleep={sleep_seconds}s"
    )


# ── auth: missing header must 401 ───────────────────────────────────


def test_nodes_health_endpoint_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # No _no_auth fixture — the real require_api_key middleware fires.
    monkeypatch.setattr("orchestrator.main.fetch_all", lambda *a, **k: [])

    from orchestrator.main import app

    client = TestClient(app)
    response = client.get("/v1/nodes/health")
    assert response.status_code == 401


# ── per-call latency: latency_ms is an integer, not float ───────────


def test_nodes_health_endpoint_latency_ms_is_int(monkeypatch: pytest.MonkeyPatch, _no_auth: None) -> None:
    monkeypatch.setattr(
        "orchestrator.main.fetch_all",
        lambda *a, **k: [_make_nodes()[0]],
    )
    monkeypatch.setattr(
        "orchestrator.main.check_health",
        lambda *a, **k: _ready_payload(),
    )
    db_calls: dict[str, Any] = {}
    _patch_connect(monkeypatch, db_calls)

    from orchestrator.main import app

    client = TestClient(app)
    response = client.get("/v1/nodes/health", headers={"X-NETRUN-API-KEY": "test-api-key"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert isinstance(item["latency_ms"], int)
    assert item["latency_ms"] >= 0


# ── DB-write failure must not break the response ────────────────────


def test_nodes_health_endpoint_db_failure_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    nodes = [_make_nodes()[0]]  # one reachable node so we attempt the UPDATE
    monkeypatch.setattr("orchestrator.main.fetch_all", lambda *a, **k: nodes)
    monkeypatch.setattr(
        "orchestrator.main.check_health",
        lambda *a, **k: _ready_payload(),
    )

    @contextmanager
    def boom_connect():  # type: ignore[no-untyped-def]
        raise RuntimeError("database down")
        yield  # pragma: no cover  # appease type-checker; never reached

    monkeypatch.setattr("orchestrator.main.connect", boom_connect)

    from orchestrator.main import app

    client = TestClient(app)
    response = client.get("/v1/nodes/health", headers={"X-NETRUN-API-KEY": "test-api-key"})
    # Endpoint still returns 200 — heartbeat update is best-effort.
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["items"][0]["reachable"] is True


# ── all nodes reachable → bulk UPDATE id list matches ──────────────


def test_nodes_health_endpoint_heartbeat_includes_all_reachable_ids(
    monkeypatch: pytest.MonkeyPatch, _no_auth: None
) -> None:
    nodes = _make_nodes()
    monkeypatch.setattr("orchestrator.main.fetch_all", lambda *a, **k: nodes)
    monkeypatch.setattr(
        "orchestrator.main.check_health",
        lambda *a, **k: _ready_payload(),
    )

    db_calls: dict[str, Any] = {}
    _patch_connect(monkeypatch, db_calls)

    from orchestrator.main import app

    client = TestClient(app)
    response = client.get("/v1/nodes/health", headers={"X-NETRUN-API-KEY": "test-api-key"})
    assert response.status_code == 200
    updates = [params for query, params in db_calls.get("calls", []) if "last_heartbeat_at" in query]
    assert len(updates) == 1
    (id_list,) = updates[0]
    assert sorted(id_list) == ["node-degraded", "node-down", "node-up"]
