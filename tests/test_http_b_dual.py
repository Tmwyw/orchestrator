"""Wave HTTP.B — dual-proxy ingest (collapse) + node_client proxyType.

The collapse tests are the stock-correctness guard: a dual node report
carries TWO URIs per IP (socks5 + paired http at socks_port-10000); they
MUST fold into ONE inventory row per IP (the pool must not double).
"""

from __future__ import annotations

import httpx
import pytest

from orchestrator import node_client
from orchestrator.jobs import collapse_dual_items


def _dual_report(n: int, *, base_socks: int = 32000) -> list[dict]:
    """Build an HTTP.A-style dual report: per IP a socks5 item at
    base_socks+i and a paired http item at (socks-10000), interleaved."""
    items: list[dict] = []
    for i in range(n):
        sp = base_socks + i
        items.append(
            {"host": "1.2.3.4", "port": sp, "login": f"u{i}", "password": f"p{i}", "protocol": "socks5"}
        )
        items.append(
            {"host": "1.2.3.4", "port": sp - 10000, "login": f"u{i}", "password": f"p{i}", "protocol": "http"}
        )
    return items


# === collapse_dual_items — pool must NOT double ===


def test_collapse_dual_two_uris_become_one_row() -> None:
    items = _dual_report(3)  # 6 report items (2 per IP)
    logical = collapse_dual_items(items)
    # 3 IPs → 3 logical rows (NOT 6 → pool not doubled).
    assert len(logical) == 3
    for i, row in enumerate(logical):
        sp = 32000 + i
        assert row["port"] == sp
        assert row["http_port"] == sp - 10000  # paired http port
        assert row["protocol"] == "socks5"  # canonical row is the socks one


def test_collapse_socks5_only_report_http_port_none() -> None:
    """Backward-compat: a pre-HTTP.A node-agent reports socks5-only (no
    http items, possibly no protocol tag) → http_port None, no doubling."""
    items = [
        {"host": "1.2.3.4", "port": 32000, "login": "u0", "password": "p0"},  # no protocol tag
        {"host": "1.2.3.4", "port": 32001, "login": "u1", "password": "p1", "protocol": "socks5"},
    ]
    logical = collapse_dual_items(items)
    assert len(logical) == 2
    assert all(row["http_port"] is None for row in logical)


def test_collapse_http_without_socks_partner_dropped() -> None:
    """An orphan http item (no matching socks at port+10000) is never
    emitted as its own inventory row."""
    items = [
        {"host": "1.2.3.4", "port": 22000, "login": "u", "password": "p", "protocol": "http"},
    ]
    assert collapse_dual_items(items) == []


def test_collapse_pairs_by_host_and_port_arithmetic() -> None:
    """http pairs to socks on the SAME host where http == socks-10000."""
    items = [
        {"host": "1.2.3.4", "port": 32000, "login": "u", "password": "p", "protocol": "socks5"},
        {"host": "9.9.9.9", "port": 22000, "login": "x", "password": "y", "protocol": "http"},  # other host
    ]
    logical = collapse_dual_items(items)
    assert len(logical) == 1
    # No same-host http partner → http_port stays None.
    assert logical[0]["http_port"] is None


# === node_client.generate — proxyType plumbing ===


def _install_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real_init = httpx.Client.__init__

    def _patched_init(self: httpx.Client, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _patched_init)


def _capture_generate_payload(monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> dict:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen.update(_json.loads(request.content))
        return httpx.Response(200, json={"success": True, "status": "ready", "items": []})

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    node_client.generate(
        url="http://node",
        api_key="k",
        job_id="j1",
        count=5,
        start_port=32000,
        timeout_sec=10,
        **kwargs,
    )
    return seen


def test_generate_default_proxy_type_socks5(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _capture_generate_payload(monkeypatch)
    assert payload["proxyType"] == "socks5"  # backward-compat default
    assert payload["startPort"] == 32000


def test_generate_dual_proxy_type(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _capture_generate_payload(monkeypatch, proxy_type="dual")
    assert payload["proxyType"] == "dual"
    # start_port comes from the per-node allocator (>=32000) → clears the
    # node's dual guard (>=15000) and yields http = socks - 10000.
    assert payload["startPort"] >= 15000
