"""Tests for node_client.generate() — Wave C-DNS pool wiring.

Covers the three branches:
- pool >= floor → ``generatorArgs`` includes ``--dns-pool <csv>``
- pool below floor → ``generatorArgs`` is omitted (script falls back)
- geo_code missing → no DNS-pool query attempted
"""

from __future__ import annotations

import httpx
import pytest

from orchestrator import node_client


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    """Force every ``httpx.Client`` created in this test to use the mock."""
    real_init = httpx.Client.__init__

    def _patched_init(self: httpx.Client, *args: object, **kwargs: object) -> None:
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", _patched_init)


def _ok_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": True, "status": "ready", "items": []},
    )


def test_generate_attaches_dns_pool_when_pool_large_enough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setattr(
        "orchestrator.dns_pool.select_pool_for_geo",
        lambda geo, limit=10: ["1.1.1.10", "2.2.2.20", "3.3.3.30", "4.4.4.40", "5.5.5.50"],
    )

    node_client.generate(
        url="http://node-x:8085",
        api_key="k1",
        job_id="job-1",
        count=10,
        start_port=30000,
        timeout_sec=60,
        geo_code="JP",
    )

    body = seen[0].read()
    import json as _json

    payload = _json.loads(body)
    assert "generatorArgs" in payload
    args = payload["generatorArgs"]
    assert "--dns-pool" in args
    csv_value = args[args.index("--dns-pool") + 1]
    assert csv_value == "1.1.1.10,2.2.2.20,3.3.3.30,4.4.4.40,5.5.5.50"


def test_generate_falls_back_when_pool_too_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool below the min floor → no ``--dns-pool`` flag, script auto-picks."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    _install_transport(monkeypatch, httpx.MockTransport(handler))
    monkeypatch.setattr(
        "orchestrator.dns_pool.select_pool_for_geo",
        lambda geo, limit=10: ["1.1.1.10", "2.2.2.20"],
    )

    node_client.generate(
        url="http://node-x:8085",
        api_key=None,
        job_id="job-2",
        count=5,
        start_port=30000,
        timeout_sec=60,
        geo_code="JP",
    )

    import json as _json

    payload = _json.loads(seen[0].read())
    assert "generatorArgs" not in payload


def test_generate_skips_pool_lookup_when_geo_code_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[httpx.Request] = []
    pool_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    def _spy_select(geo: str, limit: int = 10) -> list[str]:
        pool_calls.append(geo)
        return ["1.1.1.1"] * 10

    monkeypatch.setattr("orchestrator.dns_pool.select_pool_for_geo", _spy_select)

    node_client.generate(
        url="http://node-x:8085",
        api_key=None,
        job_id="job-3",
        count=5,
        start_port=30000,
        timeout_sec=60,
        # geo_code omitted
    )

    import json as _json

    payload = _json.loads(seen[0].read())
    assert "generatorArgs" not in payload
    assert pool_calls == []


def test_generate_handles_pool_lookup_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB error during pool lookup must not break generation — fall back."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _ok_response()

    _install_transport(monkeypatch, httpx.MockTransport(handler))

    def _boom(geo: str, limit: int = 10) -> list[str]:
        raise RuntimeError("db_unreachable")

    monkeypatch.setattr("orchestrator.dns_pool.select_pool_for_geo", _boom)

    # Should not raise
    node_client.generate(
        url="http://node-x:8085",
        api_key=None,
        job_id="job-4",
        count=5,
        start_port=30000,
        timeout_sec=60,
        geo_code="JP",
    )

    import json as _json

    payload = _json.loads(seen[0].read())
    assert "generatorArgs" not in payload
