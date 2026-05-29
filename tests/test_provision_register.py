"""POST /v1/nodes/register + provision.complete_registration. Wave PROVISION-1 ②.

No live DB: connect() and the provision/vultr helpers are mocked.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

from orchestrator import provision

# ── helpers ───────────────────────────────────────────────────────────────────


def _fake_connect(*, fetchall=None, fetchone=None, recorder: list[Any] | None = None):
    fa = list(fetchall or [])
    fo = list(fetchone or [])
    rec_list = recorder if recorder is not None else []
    cur = MagicMock()
    cur.execute = MagicMock(side_effect=lambda sql, params=None: rec_list.append((sql, params)))
    cur.fetchall = MagicMock(side_effect=lambda: fa.pop(0) if fa else [])
    cur.fetchone = MagicMock(side_effect=lambda: fo.pop(0) if fo else None)
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)

    @contextmanager
    def factory():
        yield conn

    return factory


def _sql_blob(recorder: list[Any]) -> str:
    return "\n".join(s for s, _ in recorder).lower()


# ── pure helpers ────────────────────────────────────────────────────────────


def test_hash_secret_stable() -> None:
    assert provision.hash_secret("abc") == provision.hash_secret("abc")
    assert provision.hash_secret("abc") != provision.hash_secret("abd")
    assert len(provision.hash_secret("abc")) == 64  # sha256 hex


def test_node_id_deterministic_by_ip() -> None:
    assert provision.node_id_for_ip("1.2.3.4") == provision.node_id_for_ip("1.2.3.4")
    assert provision.node_id_for_ip("1.2.3.4") != provision.node_id_for_ip("1.2.3.5")


# ── complete_registration (steps 3-6) ─────────────────────────────────────────


def test_complete_registration_existing_sku(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: list[Any] = []
    # geo lookup returns one existing active SKU -> no create
    monkeypatch.setattr(provision, "connect", _fake_connect(fetchall=[[{"id": 5}]], recorder=rec))
    job = {"job_id": "J1", "account_id": 2, "geo": "DE", "target_stock": 4000}

    out = provision.complete_registration(job=job, ip="203.0.113.7", vultr_instance_id="iid-9", log_tail="ok")

    blob = _sql_blob(rec)
    assert "insert into nodes" in blob
    assert "insert into sku_node_bindings" in blob
    assert "update proxy_inventory" in blob and "archived" in blob
    assert "update node_provisions" in blob and "registered" in blob
    assert "insert into skus" not in blob  # existing SKU -> no create
    assert out["bound_skus"] == [5]
    assert out["vultr_instance_id"] == "iid-9"
    assert out["node_id"] == provision.node_id_for_ip("203.0.113.7")


def test_complete_registration_creates_sku_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    rec: list[Any] = []
    # geo lookup returns [] -> create path; the create RETURNING id -> 9
    monkeypatch.setattr(
        provision, "connect", _fake_connect(fetchall=[[]], fetchone=[{"id": 9}], recorder=rec)
    )
    job = {"job_id": "J2", "account_id": 1, "geo": "ZZ", "target_stock": 1234}

    out = provision.complete_registration(job=job, ip="198.51.100.5", vultr_instance_id=None, log_tail="")

    blob = _sql_blob(rec)
    assert "insert into skus" in blob  # default SKU created
    assert "dualstack" in blob.lower() or any("dualstack" in str(p).lower() for _, p in rec)
    assert out["bound_skus"] == [9]


# ── endpoint ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "k")


def _client():
    from fastapi.testclient import TestClient

    from orchestrator.main import app

    return TestClient(app)


def _body(**over: Any) -> dict[str, Any]:
    base = {
        "ip": "203.0.113.10",
        "secret": "supersecret-token",
        "install_result": {"ok": True, "exit_code": 0, "log_tail": "done"},
        "hostname": "node-x",
        "agent_version": "abc1234",
    }
    base.update(over)
    return base


def test_register_unknown_secret_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision, "lookup_provision_job", lambda secret: None)
    r = _client().post("/v1/nodes/register", json=_body())
    assert r.status_code == 401
    assert r.json()["error"] == "secret_not_recognized"


def test_register_install_failed_marks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision, "lookup_provision_job", lambda secret: {"job_id": "J", "account_id": 1})
    called: dict[str, Any] = {}
    monkeypatch.setattr(
        provision,
        "mark_provision_failed",
        lambda **kw: called.update(kw),
    )
    r = _client().post(
        "/v1/nodes/register",
        json=_body(install_result={"ok": False, "exit_code": 3, "log_tail": "boom"}),
    )
    assert r.status_code == 200
    assert r.json() == {"ok": False, "job_id": "J", "status": "failed"}
    assert called["exit_code"] == 3 and called["log_tail"] == "boom"


def test_register_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        provision, "lookup_provision_job",
        lambda secret: {"job_id": "J9", "account_id": 4, "geo": "DE", "target_stock": 4000},
    )
    monkeypatch.setattr(
        provision, "complete_registration",
        lambda **kw: {"node_id": "nid", "geo": "DE", "bound_skus": [1], "vultr_instance_id": kw["vultr_instance_id"]},
    )

    from orchestrator import vultr

    class _FakeClient:
        async def find_instance_id_by_main_ip(self, ip: str) -> str:
            return "iid-found"

    async def _fake_client_for_account(account_id: int):
        assert account_id == 4
        return _FakeClient()

    monkeypatch.setattr(vultr, "client_for_account", _fake_client_for_account)

    r = _client().post("/v1/nodes/register", json=_body())
    body = r.json()
    assert r.status_code == 200
    assert body["ok"] is True and body["status"] == "registered"
    assert body["vultr_instance_id"] == "iid-found"


def test_register_survives_instance_lookup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator import vultr
    from orchestrator.crypto import FernetKeyError

    monkeypatch.setattr(
        provision, "lookup_provision_job",
        lambda secret: {"job_id": "J", "account_id": 1, "geo": "DE", "target_stock": 4000},
    )
    captured: dict[str, Any] = {}

    def _complete(**kw):
        captured.update(kw)
        return {"node_id": "n", "geo": "DE", "bound_skus": [], "vultr_instance_id": kw["vultr_instance_id"]}

    monkeypatch.setattr(provision, "complete_registration", _complete)

    async def _boom(account_id: int):
        raise FernetKeyError("fernet_key_not_configured")

    monkeypatch.setattr(vultr, "client_for_account", _boom)

    r = _client().post("/v1/nodes/register", json=_body())
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert captured["vultr_instance_id"] is None  # lookup failed -> registered without iid
