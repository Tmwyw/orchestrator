from typing import Any

import httpx

from shared.contracts import PRODUCTION_PROFILE


def _node_headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"X-API-KEY": api_key}


def check_health(url: str, api_key: str | None, timeout_sec: int = 10) -> dict[str, Any]:
    endpoint = f"{url.rstrip('/')}/health"
    with httpx.Client(timeout=timeout_sec) as client:
        response = client.get(endpoint, headers=_node_headers(api_key))
        response.raise_for_status()
        return response.json()


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
        return response.json()
