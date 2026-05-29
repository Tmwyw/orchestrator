"""provision-prepare internals: user_data render, oneliner, job creation, and
a static sanity check on the DB-driven watchdog. Wave PROVISION-1 ②.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestrator import provision

_TEMPLATE = "#!/usr/bin/env bash\nORCH=__ORCH_URL__\nSECRET=__SECRET__\nJOB=__JOB_ID__\n"


@pytest.fixture()
def _template_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    f = tmp_path / "cloud-init.sh.tmpl"
    f.write_text(_TEMPLATE, encoding="utf-8")
    monkeypatch.setenv("CLOUD_INIT_TEMPLATE_PATH", str(f))
    return f


def test_render_user_data_substitutes_markers(_template_file: Path) -> None:
    out = provision.render_user_data(orch_url="https://orch.test/", secret="SEC", job_id="J1")
    assert "ORCH=https://orch.test" in out  # trailing slash stripped
    assert "SECRET=SEC" in out
    assert "JOB=J1" in out
    assert "__ORCH_URL__" not in out and "__SECRET__" not in out


def test_build_oneliner_round_trips(_template_file: Path) -> None:
    rendered = provision.render_user_data(orch_url="https://o", secret="s", job_id="j")
    one = provision.build_oneliner(rendered)
    assert one.startswith("echo ") and "base64 -d | sudo bash" in one
    packed = one.split()[1]
    assert base64.b64decode(packed).decode() == rendered


def test_create_provision_job(monkeypatch: pytest.MonkeyPatch, _template_file: Path) -> None:
    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", "https://orch.test")
    monkeypatch.setattr(provision, "fetch_one", lambda q, p=None: {"id": 2, "enabled": True})

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cur)

    @contextmanager
    def fake_connect():
        yield conn

    monkeypatch.setattr(provision, "connect", fake_connect)

    result = provision.create_provision_job(
        account_id=2, geo="DE", region="fra", plan="vc2-1c-1gb", target_stock=4000
    )
    assert len(result["job_id"]) == 32  # uuid4 hex
    assert result["secret"]  # plaintext returned once
    assert "SECRET=" + result["secret"] in result["cloud_init_user_data"]
    assert "JOB=" + result["job_id"] in result["cloud_init_user_data"]
    # the inserted row stored only the HASH, never the plaintext
    insert_sql, insert_params = cur.execute.call_args[0]
    assert "shared_secret_hash" in insert_sql
    assert provision.hash_secret(result["secret"]) in insert_params
    assert result["secret"] not in insert_params


def test_create_provision_job_account_not_found(monkeypatch: pytest.MonkeyPatch, _template_file: Path) -> None:
    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", "https://orch.test")
    monkeypatch.setattr(provision, "fetch_one", lambda q, p=None: None)
    with pytest.raises(LookupError):
        provision.create_provision_job(account_id=9, geo="DE", region=None, plan=None, target_stock=4000)


def test_create_provision_job_requires_base_url(monkeypatch: pytest.MonkeyPatch, _template_file: Path) -> None:
    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", "")
    monkeypatch.setattr(provision, "fetch_one", lambda q, p=None: {"id": 1, "enabled": True})
    with pytest.raises(RuntimeError):
        provision.create_provision_job(account_id=1, geo="DE", region=None, plan=None, target_stock=4000)


# ── watchdog static sanity (bash; behaviour-checked manually on prod) ─────────


def test_watchdog_is_db_driven_and_reboots_via_orchestrator() -> None:
    sh = (Path(__file__).resolve().parent.parent / "scripts" / "vultr_node_watchdog.sh").read_text(
        encoding="utf-8"
    )
    # pulls nodes from the DB, not a hardcoded `declare -A NODES[...]` map
    assert "NODES[" not in sh
    assert "from nodes" in sh and "psql" in sh
    # reboots THROUGH the orchestrator (no Fernet/Vultr key in bash)
    assert "/v1/admin/nodes/" in sh and "/reboot" in sh
    assert "ORCHESTRATOR_API_KEY" in sh
    # keeps per-node fail counters
    assert ".fails" in sh
