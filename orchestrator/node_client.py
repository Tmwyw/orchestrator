from typing import Any, cast

import httpx

from shared.contracts import PRODUCTION_PROFILE


class NodeAgentError(Exception):
    """Raised when a node-agent call fails (network, 4xx, 5xx, or shape error).

    Carries an optional ``status_code`` so callers (polling worker) can
    distinguish transient 5xx from semantic 4xx (e.g. 404 port_not_found).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _node_headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"X-API-KEY": api_key}


def check_health(url: str, api_key: str | None, timeout_sec: int = 10) -> dict[str, Any]:
    endpoint = f"{url.rstrip('/')}/health"
    with httpx.Client(timeout=timeout_sec) as client:
        response = client.get(endpoint, headers=_node_headers(api_key))
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


def describe(url: str, api_key: str | None, timeout_sec: int = 15) -> dict[str, Any]:
    """GET /describe on the node-agent. Returns the JSON payload."""
    endpoint = f"{url.rstrip('/')}/describe"
    with httpx.Client(timeout=timeout_sec) as client:
        response = client.get(endpoint, headers=_node_headers(api_key))
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


def generate(
    *,
    url: str,
    api_key: str | None,
    job_id: str,
    count: int,
    start_port: int,
    timeout_sec: int,
    profile: dict[str, Any] | None = None,
    proxy_type: str = "socks5",
) -> dict[str, Any]:
    """Wave HTTP.B — ``proxy_type`` defaults to socks5 (backward-compat:
    the simple/pergb generate path is unchanged). The per-piece refill
    path passes ``"dual"`` so each IP gets a socks5 + paired http listener
    (http = socks - 10000); a pre-HTTP.A node-agent ignores the field and
    still returns socks5-only.

    NOTE: ``start_port`` comes from the per-node sequential allocator
    (ORCHESTRATOR_START_PORT_MIN=32000), which already clears the node's
    dual guard (>= 15000) and yields http = socks - 10000 in [22000+).
    """
    endpoint = f"{url.rstrip('/')}/generate"
    profile = profile or PRODUCTION_PROFILE
    payload = {
        "jobId": job_id,
        "proxyCount": count,
        "startPort": start_port,
        "proxyType": proxy_type,
        "random": True,
        "ipv6Policy": profile["ipv6_policy"],
        "networkProfile": profile["network_profile"],
        "fingerprintProfileVersion": profile["fingerprint_profile_version"],
        "intendedClientOsProfile": profile["intended_client_os_profile"],
        "clientOsProfileEnforcement": profile["client_os_profile_enforcement"],
        "actualClientProfile": profile["actual_client_profile"],
        "effectiveClientOsProfile": profile["effective_client_os_profile"],
        "generatorScript": "/opt/netrun/node_runtime/soft/generator/proxyyy_automated.sh",
        "timeoutSec": timeout_sec,
    }
    with httpx.Client(timeout=timeout_sec + 30) as client:
        response = client.post(endpoint, json=payload, headers=_node_headers(api_key))
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


# === Pay-per-GB endpoints (Wave B-8.2) ===


# Node-agent encodes requested ports in the GET query string
# (?ports=p1,p2,...). A node with a large pool (1000+ ports) overflows the
# node-agent's request-line/header limit → HTTP 431 Request Header Fields Too
# Large → the WHOLE node's accounting fails and pay-per-GB usage never advances
# (observed in prod 2026-06: a ~1500-port node returned 431 every cycle,
# bytes_observed_total=0). Chunk the ports so each request URL stays small.
_ACCOUNTING_PORT_CHUNK = 100


def get_accounting(
    url: str,
    api_key: str | None,
    ports: list[int],
    timeout_sec: int = 10,
) -> dict[str, dict[str, int]]:
    """GET /accounting?ports=PORT[,PORT...] per design § 3.1, chunked.

    Returns ``{port_str: {bytes_in, bytes_out, bytes_in6, bytes_out6}}``.
    Requested ports are split into ``_ACCOUNTING_PORT_CHUNK``-sized batches
    (one GET each, results merged) so a large pool can't overflow the
    node-agent's URL/header limit (HTTP 431). The node-agent's defensive
    contract may return a 200 with a partial map (only some of the requested
    ports) — caller is expected to handle that. Raises ``NodeAgentError`` on
    transport failure, 5xx, or 4xx of ANY chunk.
    """
    if not ports:
        return {}
    endpoint = f"{url.rstrip('/')}/accounting"
    merged: dict[str, dict[str, int]] = {}
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            for start in range(0, len(ports), _ACCOUNTING_PORT_CHUNK):
                chunk = ports[start : start + _ACCOUNTING_PORT_CHUNK]
                params = {"ports": ",".join(str(p) for p in chunk)}
                response = client.get(endpoint, params=params, headers=_node_headers(api_key))
                if response.status_code != 200:
                    raise NodeAgentError(
                        f"accounting_status_{response.status_code}",
                        status_code=response.status_code,
                    )
                try:
                    body = response.json()
                except ValueError as exc:
                    raise NodeAgentError(f"accounting_invalid_json: {exc}") from exc
                counters = body.get("counters") if isinstance(body, dict) else None
                # Newer node-agent wraps as {"success": true, "counters": {...}};
                # older variants returned the bare map. Accept both shapes.
                chunk_map = counters if counters is not None else body
                if isinstance(chunk_map, dict):
                    merged.update(cast(dict[str, dict[str, int]], chunk_map))
    except httpx.HTTPError as exc:
        raise NodeAgentError(f"accounting_request_failed: {exc}") from exc
    return merged


def post_disable(
    url: str,
    api_key: str | None,
    port: int,
    timeout_sec: int = 10,
) -> dict[str, Any]:
    """POST /accounts/{port}/disable per design § 3.2 (idempotent).

    Already-disabled returns 200 (no-op). Raises ``NodeAgentError`` with
    ``status_code=404`` on port_not_found, or other status codes on failure.
    """
    endpoint = f"{url.rstrip('/')}/accounts/{int(port)}/disable"
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            response = client.post(endpoint, headers=_node_headers(api_key))
    except httpx.HTTPError as exc:
        raise NodeAgentError(f"disable_request_failed: {exc}") from exc
    if response.status_code != 200:
        raise NodeAgentError(
            f"disable_status_{response.status_code}",
            status_code=response.status_code,
        )
    try:
        return cast(dict[str, Any], response.json())
    except ValueError:
        return {}


def post_enable(
    url: str,
    api_key: str | None,
    port: int,
    timeout_sec: int = 10,
) -> dict[str, Any]:
    """POST /accounts/{port}/enable per design § 3.3 (idempotent)."""
    endpoint = f"{url.rstrip('/')}/accounts/{int(port)}/enable"
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            response = client.post(endpoint, headers=_node_headers(api_key))
    except httpx.HTTPError as exc:
        raise NodeAgentError(f"enable_request_failed: {exc}") from exc
    if response.status_code != 200:
        raise NodeAgentError(
            f"enable_status_{response.status_code}",
            status_code=response.status_code,
        )
    try:
        return cast(dict[str, Any], response.json())
    except ValueError:
        return {}
