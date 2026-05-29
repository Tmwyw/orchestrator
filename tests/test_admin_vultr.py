"""Vultr accounts CRUD + provision-prepare endpoints. Wave PROVISION-1 ②."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet

from orchestrator import admin_vultr


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "k")
    monkeypatch.setenv("ORCH_FERNET_KEY", Fernet.generate_key().decode())


def _client():
    from fastapi.testclient import TestClient

    from orchestrator.main import app

    return TestClient(app, headers={"X-NETRUN-API-KEY": "k"})


# ── accounts CRUD ─────────────────────────────────────────────────────────────


def test_create_account_encrypts_and_masks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_fetch_one(query: str, params: Any = None):
        calls["n"] += 1
        if "select id from vultr_accounts where label" in query:
            return None  # no existing
        if "insert into vultr_accounts" in query:
            return {"id": 1, "label": "acct-a", "enabled": True}
        return None

    monkeypatch.setattr(admin_vultr, "fetch_one", fake_fetch_one)
    r = _client().post("/v1/admin/vultr-accounts", json={"label": "acct-a", "api_key": "VULTRKEY1234"})
    assert r.status_code == 201
    body = r.json()
    assert body["label"] == "acct-a"
    assert body["key_masked"] == "****1234"  # never the plaintext


def test_create_account_requires_fields() -> None:
    r = _client().post("/v1/admin/vultr-accounts", json={"label": "x"})
    assert r.status_code == 400


def test_create_account_duplicate_label_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_vultr, "fetch_one", lambda q, p=None: {"id": 1})
    r = _client().post("/v1/admin/vultr-accounts", json={"label": "dup", "api_key": "K12345678"})
    assert r.status_code == 409


def test_list_accounts_masks_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    from orchestrator.crypto import encrypt_secret

    enc = encrypt_secret("SECRETKEYABCD")
    monkeypatch.setattr(
        admin_vultr,
        "fetch_all",
        lambda q, p=None: [
            {"id": 1, "label": "a", "api_key_enc": enc, "enabled": True, "created_at": "t", "updated_at": "t"}
        ],
    )
    r = _client().get("/v1/admin/vultr-accounts")
    assert r.status_code == 200
    acct = r.json()["accounts"][0]
    assert acct["key_masked"] == "****ABCD"
    assert "api_key_enc" not in acct  # ciphertext not leaked either


def test_disable_account(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_vultr, "fetch_one", lambda q, p=None: {"id": 3})
    executed: list[Any] = []
    monkeypatch.setattr(admin_vultr, "execute", lambda q, p=None: executed.append((q, p)))
    r = _client().delete("/v1/admin/vultr-accounts/3")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert "enabled = false" in executed[0][0].lower()


def test_patch_account_no_fields_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_vultr, "fetch_one", lambda q, p=None: {"id": 1})
    r = _client().patch("/v1/admin/vultr-accounts/1", json={})
    assert r.status_code == 400


# ── provision-prepare ─────────────────────────────────────────────────────────


def test_provision_prepare_returns_job_secret_userdata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        admin_vultr,
        "create_provision_job",
        lambda **kw: {
            "job_id": "JOB123",
            "secret": "PLAINSECRET",
            "cloud_init_user_data": "#!/usr/bin/env bash\n# ...__ORCH_URL__ substituted...",
            "oneliner_command": "echo X | base64 -d | sudo bash",
        },
    )
    r = _client().post(
        "/v1/admin/nodes/provision-prepare",
        json={"account_id": 2, "geo": "DE", "target_stock": 4000},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["job_id"] == "JOB123"
    assert body["secret"] == "PLAINSECRET"
    assert "cloud_init_user_data" in body
    assert body["oneliner_command"].startswith("echo ")


def test_provision_prepare_requires_account_and_geo() -> None:
    c = _client()
    assert c.post("/v1/admin/nodes/provision-prepare", json={"geo": "DE"}).status_code == 400
    assert c.post("/v1/admin/nodes/provision-prepare", json={"account_id": 1}).status_code == 400


def test_provision_prepare_account_not_found_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(**kw):
        raise LookupError("vultr_account_not_found:9")

    monkeypatch.setattr(admin_vultr, "create_provision_job", _boom)
    r = _client().post("/v1/admin/nodes/provision-prepare", json={"account_id": 9, "geo": "DE"})
    assert r.status_code == 404
