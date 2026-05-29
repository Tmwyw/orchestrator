"""Per-account Vultr client: right key per account, retry, lookup, reboot.

Wave PROVISION-1 ②. httpx is mocked via MockTransport (no network).
"""

from __future__ import annotations

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
async def test_find_instance_id_by_main_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer KEY-A"
        return httpx.Response(
            200,
            json={
                "instances": [
                    {"id": "iid-1", "main_ip": "203.0.113.1"},
                    {"id": "iid-2", "main_ip": "203.0.113.2"},
                ],
                "meta": {"links": {"next": ""}},
            },
        )

    _install_mock_transport(monkeypatch, handler)
    client = vultr.VultrClient("KEY-A")
    assert await client.find_instance_id_by_main_ip("203.0.113.2") == "iid-2"
    assert await client.find_instance_id_by_main_ip("203.0.113.9") is None


@pytest.mark.asyncio
async def test_client_for_account_uses_that_accounts_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Resolve account 7 -> key "KEY-SEVEN"; assert the request carries it.
    monkeypatch.setattr(
        vultr, "fetch_one", lambda *a, **k: {"api_key_enc": "enc", "enabled": True}
    )
    monkeypatch.setattr(vultr, "decrypt_secret", lambda enc: "KEY-SEVEN")

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json={"instances": [], "meta": {"links": {"next": ""}}})

    _install_mock_transport(monkeypatch, handler)
    client = await vultr.client_for_account(7)
    await client.list_instances()
    assert seen == ["Bearer KEY-SEVEN"]


@pytest.mark.asyncio
async def test_account_not_found_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vultr, "fetch_one", lambda *a, **k: None)
    with pytest.raises(vultr.VultrAccountNotFoundError):
        await vultr.client_for_account(999)


@pytest.mark.asyncio
async def test_retry_on_5xx_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"instances": [], "meta": {"links": {"next": ""}}})

    monkeypatch.setattr(vultr.asyncio, "sleep", lambda *_a, **_k: _noop())
    _install_mock_transport(monkeypatch, handler)
    client = vultr.VultrClient("K")
    assert await client.list_instances() == []
    assert calls["n"] == 3  # two 503s retried, third 200


@pytest.mark.asyncio
async def test_reboot_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/instances/iid-1/reboot")
        assert request.method == "POST"
        return httpx.Response(204)

    _install_mock_transport(monkeypatch, handler)
    await vultr.VultrClient("K").reboot("iid-1")  # no raise


@pytest.mark.asyncio
async def test_reboot_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_mock_transport(monkeypatch, lambda r: httpx.Response(404))
    with pytest.raises(vultr.VultrError):
        await vultr.VultrClient("K").reboot("missing")


async def _noop() -> None:
    return None
