"""Tests for B-8.2 node_client pergb extensions (get_accounting/disable/enable)."""

from __future__ import annotations

import httpx
import pytest

from orchestrator import node_client
from orchestrator.node_client import NodeAgentError


def _install_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> list[httpx.Request]:
    """Replace httpx.Client construction so it always uses the mock transport.

    Returns a list that records every Request seen by the transport (mutated
    by the handler closure that callers build in tests).
    """
    real_init = httpx.Client.__init__

    def _patched_init(self: httpx.Client, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _patched_init)
    return []


# === get_accounting ===


def test_get_accounting_happy_wrapped_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """node_runtime today wraps the response as {success, counters: {...}}."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "counters": {
                    "32001": {"bytes_in": 100, "bytes_out": 200, "bytes_in6": 10, "bytes_out6": 20},
                    "32002": {"bytes_in": 0, "bytes_out": 0, "bytes_in6": 0, "bytes_out6": 0},
                },
            },
        )

    transport = httpx.MockTransport(handler)
    _install_transport(monkeypatch, transport)

    result = node_client.get_accounting("http://node-x:8085", "k1", [32001, 32002])

    assert "32001" in result
    assert result["32001"]["bytes_in"] == 100
    assert result["32001"]["bytes_in6"] == 10
    assert seen[0].url.params["ports"] == "32001,32002"
    assert seen[0].headers["X-API-KEY"] == "k1"


def test_get_accounting_happy_bare_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defensive: bare-map response is also accepted."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"32001": {"bytes_in": 1, "bytes_out": 2, "bytes_in6": 3, "bytes_out6": 4}},
        )

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = node_client.get_accounting("http://node-x:8085", None, [32001])

    assert result["32001"]["bytes_out6"] == 4


def test_get_accounting_no_api_key_omits_header(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"counters": {}})

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    node_client.get_accounting("http://node-x:8085", None, [32001])

    assert "X-API-KEY" not in seen[0].headers


def test_get_accounting_empty_ports_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """No HTTP call is made when ports list is empty."""
    called: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        called.append(True)
        return httpx.Response(200, json={})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    assert node_client.get_accounting("http://node-x:8085", "k", []) == {}
    assert called == []


def test_get_accounting_5xx_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "nft_busy"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(NodeAgentError) as ei:
        node_client.get_accounting("http://node-x:8085", None, [32001])
    assert ei.value.status_code == 503


def test_get_accounting_404_raises_with_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "ports_not_found"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(NodeAgentError) as ei:
        node_client.get_accounting("http://node-x:8085", None, [32001])
    assert ei.value.status_code == 404


def test_get_accounting_transport_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(NodeAgentError) as ei:
        node_client.get_accounting("http://node-x:8085", None, [32001])
    # No status_code on transport-level failures
    assert ei.value.status_code is None


def test_get_accounting_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(NodeAgentError, match="invalid_json"):
        node_client.get_accounting("http://node-x:8085", None, [32001])


# === post_disable / post_enable ===


def test_post_disable_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True, "port": 32001, "action": "killed"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = node_client.post_disable("http://node-x:8085", "k1", 32001)

    assert result["action"] == "killed"
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/accounts/32001/disable"


def test_post_disable_already_disabled_returns_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent contract: calling disable on already-disabled port → 200."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"action": "already_disabled"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = node_client.post_disable("http://node-x:8085", None, 32001)
    assert result["action"] == "already_disabled"


def test_post_disable_404_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "port_not_found"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(NodeAgentError) as ei:
        node_client.post_disable("http://node-x:8085", None, 99999)
    assert ei.value.status_code == 404


def test_post_enable_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"action": "started", "pid": 1234})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    result = node_client.post_enable("http://node-x:8085", "k1", 32001)
    assert result["action"] == "started"
    assert seen[0].url.path == "/accounts/32001/enable"


def test_post_enable_404_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "port_not_found"})

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(NodeAgentError) as ei:
        node_client.post_enable("http://node-x:8085", None, 32001)
    assert ei.value.status_code == 404
