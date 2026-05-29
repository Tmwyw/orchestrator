"""Per-account Vultr API client. Wave PROVISION-1 ②.

Multiple EQUAL Vultr accounts: each call resolves the account's Fernet-encrypted
key from vultr_accounts and talks to the Vultr v2 API with THAT key (a key only
sees its own instances). Replaces the single-key reboot path in admin_nodes.py.

httpx with bounded retry on 429 / 5xx. All network is async; the only sync bit
(DB read + Fernet decrypt) is wrapped by callers in asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from orchestrator.crypto import decrypt_secret
from orchestrator.db import fetch_one

VULTR_API = "https://api.vultr.com/v2"
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


class VultrError(RuntimeError):
    """Vultr API / account resolution failure."""


class VultrAccountNotFoundError(VultrError):
    """No vultr_accounts row for the given id."""


def _account_api_key(account_id: int) -> str:
    """Resolve + decrypt the account's Vultr key (sync; wrap in to_thread)."""
    row = fetch_one(
        "select api_key_enc, enabled from vultr_accounts where id = %s", (account_id,)
    )
    if not row:
        raise VultrAccountNotFoundError(f"vultr_account_not_found:{account_id}")
    return decrypt_secret(str(row["api_key_enc"]))


class VultrClient:
    """Thin Vultr v2 client bound to one account's API key."""

    def __init__(self, api_key: str, *, timeout: float = 20.0) -> None:
        self._key = api_key
        self._timeout = timeout

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    async def _request(
        self, client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
    ) -> httpx.Response:
        url = f"{VULTR_API}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                resp = await client.request(method, url, headers=self._headers, **kwargs)
            except httpx.HTTPError as exc:  # network-level
                last_exc = exc
            else:
                if resp.status_code not in _RETRY_STATUS:
                    return resp
                last_exc = VultrError(f"vultr_{resp.status_code}")
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
        raise VultrError(f"vultr_request_failed:{method} {path}") from last_exc

    async def list_instances(self) -> list[dict[str, object]]:
        """All instances visible to this account (paginated by cursor)."""
        out: list[dict[str, object]] = []
        cursor = ""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for _ in range(20):  # defensive page cap
                params: dict[str, object] = {"per_page": 100}
                if cursor:
                    params["cursor"] = cursor
                resp = await self._request(client, "GET", "/instances", params=params)
                if resp.status_code != 200:
                    raise VultrError(f"vultr_list_failed:{resp.status_code}")
                data = resp.json()
                out.extend(data.get("instances", []))
                cursor = ((data.get("meta") or {}).get("links") or {}).get("next") or ""
                if not cursor:
                    break
        return out

    async def find_instance_id_by_main_ip(self, ip: str) -> str | None:
        """Vultr instance id whose main_ip == ip, or None."""
        if not ip:
            return None
        for inst in await self.list_instances():
            if inst.get("main_ip") == ip:
                return str(inst.get("id")) if inst.get("id") else None
        return None

    async def reboot(self, instance_id: str) -> None:
        """Reboot one instance; raises VultrError on non-202/204."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await self._request(client, "POST", f"/instances/{instance_id}/reboot")
        if resp.status_code not in (202, 204):
            raise VultrError(f"vultr_reboot_failed:{resp.status_code}")


async def client_for_account(account_id: int) -> VultrClient:
    """Build a VultrClient for an account (resolves + decrypts its key)."""
    key = await asyncio.to_thread(_account_api_key, account_id)
    return VultrClient(key)
