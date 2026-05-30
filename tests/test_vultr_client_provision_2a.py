"""Vultr client variant-A surface: os-id resolve, region/plan listings,
create/get/destroy instance. Wave PROVISION-2A. httpx mocked (no network)."""

from __future__ import annotations

import base64

import httpx
import pytest

from orchestrator import vultr


def _install_mock_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real(*args, **kwargs)

    monkeypatch.setattr(vultr.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_resolve_ubuntu_2404_os_id_paginates_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/os")
        pages["n"] += 1
        if not request.url.params.get("cursor"):
            return httpx.Response(
                200,
                json={
                    "os": [{"id": 100, "name": "Debian 12 x64", "arch": "x64"}],
                    "meta": {"links": {"next": "PAGE2"}},
                },
            )
        return httpx.Response(
            200,
            json={
                "os": [
                    {"id": 2284, "name": "Ubuntu 24.04 LTS x64", "arch": "x64"},
                    {"id": 2285, "name": "Ubuntu 24.04 LTS", "arch": "i386"},
                ],
                "meta": {"links": {"next": ""}},
            },
        )

    _install_mock_transport(monkeypatch, handler)
    client = vultr.VultrClient("K")
    assert await client.resolve_ubuntu_2404_os_id() == 2284
    after = pages["n"]
    # second call is served from the instance cache (no extra HTTP)
    assert await client.resolve_ubuntu_2404_os_id() == 2284
    assert pages["n"] == after


@pytest.mark.asyncio
async def test_resolve_os_id_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_transport(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"os": [{"id": 1, "name": "Fedora", "arch": "x64"}], "meta": {"links": {"next": ""}}}
        ),
    )
    with pytest.raises(vultr.VultrError):
        await vultr.VultrClient("K").resolve_ubuntu_2404_os_id()


@pytest.mark.asyncio
async def test_list_regions(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_transport(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "regions": [
                    {"id": "cdg", "city": "Paris", "country": "FR", "continent": "Europe", "options": []},
                    {"id": "ord", "city": "Chicago", "country": "US", "continent": "North America"},
                ],
                "meta": {"links": {"next": ""}},
            },
        ),
    )
    regions = await vultr.VultrClient("K").list_regions()
    assert regions[0] == {"id": "cdg", "city": "Paris", "country": "FR", "continent": "Europe"}
    assert {r["id"] for r in regions} == {"cdg", "ord"}


@pytest.mark.asyncio
async def test_list_plans_filters_and_sorts(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_transport(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "plans": [
                    # too small — dropped
                    {"id": "vc2-1c-1gb", "type": "vc2", "vcpu_count": 1, "ram": 1024, "disk": 25, "monthly_cost": 5},
                    # wrong type — dropped
                    {"id": "vbm-4c-32gb", "type": "vbm", "vcpu_count": 4, "ram": 32768, "disk": 800, "monthly_cost": 200},
                    # ok, pricier
                    {"id": "vc2-4c-8gb", "type": "vc2", "vcpu_count": 4, "ram": 8192, "disk": 180, "monthly_cost": 48, "locations": ["cdg"]},
                    # ok, cheapest -> first
                    {"id": "vc2-2c-4gb", "type": "vc2", "vcpu_count": 2, "ram": 4096, "disk": 80, "monthly_cost": 24},
                ],
                "meta": {"links": {"next": ""}},
            },
        ),
    )
    plans = await vultr.VultrClient("K").list_plans()
    assert [p["id"] for p in plans] == ["vc2-2c-4gb", "vc2-4c-8gb"]
    assert plans[0]["ram"] == 4096 and plans[0]["vcpu_count"] == 2


@pytest.mark.asyncio
async def test_create_instance_posts_body_and_returns_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/instances")
        import json as _json

        seen.update(_json.loads(request.content))
        return httpx.Response(
            202,
            json={"instance": {"id": "iid-9", "main_ip": "0.0.0.0", "status": "pending"}},
        )

    _install_mock_transport(monkeypatch, handler)
    out = await vultr.VultrClient("K").create_instance(
        region="cdg",
        plan="vc2-2c-4gb",
        os_id=2284,
        user_data_b64="QkFTRTY0",
        label="netrun-de-abcd1234",
        hostname="netrun-de-abcd1234",
    )
    assert out == {"id": "iid-9", "main_ip": "0.0.0.0", "status": "pending"}
    assert seen["region"] == "cdg"
    assert seen["os_id"] == 2284
    assert seen["user_data"] == "QkFTRTY0"
    assert seen["enable_ipv6"] is True
    assert seen["backups"] == "disabled"
    assert "sshkey_id" not in seen


@pytest.mark.asyncio
async def test_create_instance_with_sshkeys(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.update(_json.loads(request.content))
        return httpx.Response(201, json={"instance": {"id": "iid-1", "main_ip": "1.2.3.4"}})

    _install_mock_transport(monkeypatch, handler)
    await vultr.VultrClient("K").create_instance(
        region="ord", plan="p", os_id=1, user_data_b64="x", label="l", hostname="h",
        sshkey_ids=["key-a", "key-b"],
    )
    assert captured["sshkey_id"] == ["key-a", "key-b"]


@pytest.mark.asyncio
async def test_create_instance_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_transport(monkeypatch, lambda r: httpx.Response(400, json={"error": "bad plan"}))
    with pytest.raises(vultr.VultrError):
        await vultr.VultrClient("K").create_instance(
            region="x", plan="x", os_id=1, user_data_b64="x", label="l", hostname="h"
        )


@pytest.mark.asyncio
async def test_get_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/instances/iid-9")
        return httpx.Response(200, json={"instance": {"id": "iid-9", "main_ip": "5.6.7.8", "status": "active"}})

    _install_mock_transport(monkeypatch, handler)
    inst = await vultr.VultrClient("K").get_instance("iid-9")
    assert inst["main_ip"] == "5.6.7.8" and inst["status"] == "active"


@pytest.mark.asyncio
async def test_destroy_instance_ok_and_404_are_truthy(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_transport(monkeypatch, lambda r: httpx.Response(204))
    assert await vultr.VultrClient("K").destroy_instance("iid-9") is True
    _install_mock_transport(monkeypatch, lambda r: httpx.Response(404))
    assert await vultr.VultrClient("K").destroy_instance("gone") is True


@pytest.mark.asyncio
async def test_destroy_instance_hard_failure_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vultr.asyncio, "sleep", lambda *_a, **_k: _noop())
    _install_mock_transport(monkeypatch, lambda r: httpx.Response(500))
    assert await vultr.VultrClient("K").destroy_instance("iid-9") is False


def test_user_data_b64_round_trips() -> None:
    # sanity: the b64 create_instance receives decodes back to the script
    raw = "#!/usr/bin/env bash\necho hi\n"
    packed = base64.b64encode(raw.encode()).decode()
    assert base64.b64decode(packed).decode() == raw


async def _noop() -> None:
    return None
