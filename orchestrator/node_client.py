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
) -> dict[str, Any]:
    endpoint = f"{url.rstrip('/')}/generate"
    payload = {
        "jobId": job_id,
        "proxyCount": count,
        "startPort": start_port,
        "proxyType": "socks5",
        "random": True,
        "ipv6Policy": PRODUCTION_PROFILE["ipv6_policy"],
        "networkProfile": PRODUCTION_PROFILE["network_profile"],
        "fingerprintProfileVersion": PRODUCTION_PROFILE["fingerprint_profile_version"],
        "intendedClientOsProfile": PRODUCTION_PROFILE["intended_client_os_profile"],
        "clientOsProfileEnforcement": PRODUCTION_PROFILE["client_os_profile_enforcement"],
        "actualClientProfile": PRODUCTION_PROFILE["actual_client_profile"],
        "effectiveClientOsProfile": PRODUCTION_PROFILE["effective_client_os_profile"],
        "generatorScript": "/opt/netrun/node_runtime/soft/generator/proxyyy_automated.sh",
        "timeoutSec": timeout_sec,
    }
    with httpx.Client(timeout=timeout_sec + 30) as client:
        response = client.post(endpoint, json=payload, headers=_node_headers(api_key))
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


# === Pay-per-GB endpoints (Wave B-8.2) ===


def get_accounting(
    url: str,
    api_key: str | None,
    ports: list[int],
    timeout_sec: int = 10,
) -> dict[str, dict[str, int]]:
    """GET /accounting?ports=PORT[,PORT...] per design § 3.1.

    Returns ``{port_str: {bytes_in, bytes_out, bytes_in6, bytes_out6}}``.
    The node-agent's defensive contract may return a 200 with a partial map
    (only some of the requested ports) — caller is expected to handle that.
    Raises ``NodeAgentError`` on transport failure, 5xx, or 4xx.
    """
    if not ports:
        return {}
    endpoint = f"{url.rstrip('/')}/accounting"
    params = {"ports": ",".join(str(p) for p in ports)}
    try:
        with httpx.Client(timeout=timeout_sec) as client:
            response = client.get(endpoint, params=params, headers=_node_headers(api_key))
    except httpx.HTTPError as exc:
        raise NodeAgentError(f"accounting_request_failed: {exc}") from exc
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
    # Newer node-agent wraps as {"success": true, "counters": {...}}; older
    # variants returned the bare map. Accept both shapes.
    return cast(dict[str, dict[str, int]], counters if counters is not None else body)


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
