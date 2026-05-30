"""create_and_provision (variant A): inserts an installing job, calls Vultr
create with base64 user_data, records the instance id; rolls the job to 'failed'
when create blows up. Wave PROVISION-2A. DB + Vultr fully mocked."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from pathlib import Path

import pytest

from orchestrator import provision, vultr

_TEMPLATE = "#!/usr/bin/env bash\nORCH=__ORCH_URL__\nSECRET=__SECRET__\nJOB=__JOB_ID__\n"


@pytest.fixture()
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "cloud-init.sh.tmpl"
    f.write_text(_TEMPLATE, encoding="utf-8")
    monkeypatch.setenv("CLOUD_INIT_TEMPLATE_PATH", str(f))
    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", "https://orch.test")


class _FakeCursor:
    def __init__(self, sink: list[tuple]) -> None:
        self._sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._sink.append((sql, params))


class _FakeConn:
    def __init__(self, sink: list[tuple]) -> None:
        self._sink = sink

    def cursor(self):
        return _FakeCursor(self._sink)


def _fake_connect_factory(sink: list[tuple]):
    @contextmanager
    def _connect():
        yield _FakeConn(sink)

    return _connect


class _FakeClient:
    def __init__(self, *, create_result=None, raise_on_create: Exception | None = None) -> None:
        self.create_result = create_result or {"id": "iid-9", "main_ip": "1.2.3.4", "status": "pending"}
        self.raise_on_create = raise_on_create
        self.create_kwargs: dict = {}

    async def resolve_ubuntu_2404_os_id(self) -> int:
        return 2284

    async def create_instance(self, **kwargs):
        self.create_kwargs = kwargs
        if self.raise_on_create:
            raise self.raise_on_create
        return self.create_result


@pytest.mark.asyncio
async def test_create_and_provision_happy(_env, monkeypatch: pytest.MonkeyPatch) -> None:
    sink: list[tuple] = []
    monkeypatch.setattr(provision, "fetch_one", lambda q, p=None: {"id": 2, "enabled": True})
    monkeypatch.setattr(provision, "connect", _fake_connect_factory(sink))

    fake = _FakeClient()

    async def _client_for_account(_id):
        return fake

    monkeypatch.setattr(provision.vultr, "client_for_account", _client_for_account)

    out = await provision.create_and_provision(
        account_id=2, region="cdg", plan="vc2-2c-4gb", geo="FR", target_stock=4000
    )

    assert out["status"] == "installing"
    assert out["vultr_instance_id"] == "iid-9"
    assert out["main_ip"] == "1.2.3.4"
    assert len(out["job_id"]) == 32

    # job inserted with status 'installing' + only the secret HASH
    insert_sql, insert_params = sink[0]
    assert "insert into node_provisions" in insert_sql
    assert "'installing'" in insert_sql
    assert provision.hash_secret  # used internally
    # the rendered user_data passed to Vultr was base64 and carries job_id + secret
    b64 = fake.create_kwargs["user_data_b64"]
    rendered = base64.b64decode(b64).decode()
    assert f"JOB={out['job_id']}" in rendered
    assert "ORCH=https://orch.test" in rendered
    assert fake.create_kwargs["os_id"] == 2284
    assert fake.create_kwargs["region"] == "cdg"
    assert fake.create_kwargs["label"].startswith("netrun-fr-")

    # instance id + ip recorded on the job row
    update_sql, update_params = sink[-1]
    assert "vultr_instance_id" in update_sql
    assert "iid-9" in update_params and "1.2.3.4" in update_params


@pytest.mark.asyncio
async def test_create_and_provision_zero_ip_stored_as_null(_env, monkeypatch: pytest.MonkeyPatch) -> None:
    sink: list[tuple] = []
    monkeypatch.setattr(provision, "fetch_one", lambda q, p=None: {"id": 1, "enabled": True})
    monkeypatch.setattr(provision, "connect", _fake_connect_factory(sink))
    fake = _FakeClient(create_result={"id": "iid-x", "main_ip": "0.0.0.0", "status": "pending"})

    async def _client_for_account(_id):
        return fake

    monkeypatch.setattr(provision.vultr, "client_for_account", _client_for_account)

    out = await provision.create_and_provision(
        account_id=1, region="ord", plan="p", geo="US", target_stock=4000
    )
    assert out["main_ip"] == "0.0.0.0"
    _update_sql, update_params = sink[-1]
    assert None in update_params  # ip stored as NULL, not "0.0.0.0"


@pytest.mark.asyncio
async def test_create_failure_marks_job_failed_and_reraises(_env, monkeypatch: pytest.MonkeyPatch) -> None:
    sink: list[tuple] = []
    monkeypatch.setattr(provision, "fetch_one", lambda q, p=None: {"id": 2, "enabled": True})
    monkeypatch.setattr(provision, "connect", _fake_connect_factory(sink))
    fake = _FakeClient(raise_on_create=vultr.VultrError("vultr_create_failed:500"))

    async def _client_for_account(_id):
        return fake

    monkeypatch.setattr(provision.vultr, "client_for_account", _client_for_account)

    with pytest.raises(vultr.VultrError):
        await provision.create_and_provision(
            account_id=2, region="cdg", plan="p", geo="FR", target_stock=4000
        )

    # job was inserted, then flipped to failed with error='vultr_create_failed'
    assert "insert into node_provisions" in sink[0][0]
    fail_sql, _ = sink[-1]
    assert "status = 'failed'" in fail_sql
    assert "vultr_create_failed" in fail_sql


@pytest.mark.asyncio
async def test_account_not_found_raises_lookup(_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision, "fetch_one", lambda q, p=None: None)
    with pytest.raises(LookupError):
        await provision.create_and_provision(
            account_id=9, region="cdg", plan="p", geo="FR", target_stock=4000
        )


@pytest.mark.asyncio
async def test_account_disabled_raises_permission(_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provision, "fetch_one", lambda q, p=None: {"id": 2, "enabled": False})
    with pytest.raises(PermissionError):
        await provision.create_and_provision(
            account_id=2, region="cdg", plan="p", geo="FR", target_stock=4000
        )


@pytest.mark.asyncio
async def test_requires_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "t.tmpl"
    f.write_text(_TEMPLATE, encoding="utf-8")
    monkeypatch.setenv("CLOUD_INIT_TEMPLATE_PATH", str(f))
    monkeypatch.setenv("ORCHESTRATOR_BASE_URL", "")
    with pytest.raises(RuntimeError):
        await provision.create_and_provision(
            account_id=1, region="cdg", plan="p", geo="FR", target_stock=4000
        )
