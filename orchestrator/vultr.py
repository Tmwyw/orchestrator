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
        self._ubuntu_os_id: int | None = None  # resolve_ubuntu_2404_os_id cache

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

    # ── PROVISION-2A: cursor-paginated listings + create/destroy ────────────────

    async def _paged(self, path: str, key: str) -> list[dict[str, Any]]:
        """Collect every item under ``key`` across cursor pages (GET ``path``)."""
        out: list[dict[str, Any]] = []
        cursor = ""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for _ in range(50):  # defensive page cap (os list is large)
                params: dict[str, object] = {"per_page": 500}
                if cursor:
                    params["cursor"] = cursor
                resp = await self._request(client, "GET", path, params=params)
                if resp.status_code != 200:
                    raise VultrError(f"vultr_list_failed:{path}:{resp.status_code}")
                data = resp.json()
                out.extend(data.get(key, []))
                cursor = ((data.get("meta") or {}).get("links") or {}).get("next") or ""
                if not cursor:
                    break
        return out

    async def resolve_ubuntu_2404_os_id(self) -> int:
        """Vultr os.id for 'Ubuntu 24.04 LTS x64' (cached per client)."""
        if self._ubuntu_os_id is not None:
            return self._ubuntu_os_id
        for os_entry in await self._paged("/os", "os"):
            name = str(os_entry.get("name") or "")
            arch = str(os_entry.get("arch") or "")
            if "Ubuntu 24.04" in name and arch == "x64":
                os_id = int(os_entry["id"])
                self._ubuntu_os_id = os_id
                return os_id
        raise VultrError("vultr_ubuntu_2404_os_not_found")

    async def list_regions(self) -> list[dict[str, Any]]:
        """All Vultr regions as [{id, city, country, continent}] (id = slug)."""
        return [
            {
                "id": r.get("id"),
                "city": r.get("city"),
                "country": r.get("country"),
                "continent": r.get("continent"),
            }
            for r in await self._paged("/regions", "regions")
        ]

    async def list_plans(self) -> list[dict[str, Any]]:
        """Regular cloud-compute plans fit for a node (>=2 vCPU, >=4 GB RAM),
        sorted by monthly_cost ascending."""
        usable: list[dict[str, Any]] = []
        for p in await self._paged("/plans", "plans"):
            if str(p.get("type") or "") not in ("vc2", "voc"):
                continue
            if int(p.get("vcpu_count") or 0) < 2 or int(p.get("ram") or 0) < 4096:
                continue
            usable.append(
                {
                    "id": p.get("id"),
                    "vcpu_count": p.get("vcpu_count"),
                    "ram": p.get("ram"),
                    "disk": p.get("disk"),
                    "monthly_cost": p.get("monthly_cost"),
                    "type": p.get("type"),
                    "locations": p.get("locations") or [],
                }
            )
        usable.sort(key=lambda p: float(p.get("monthly_cost") or 0))
        return usable

    async def create_instance(
        self,
        *,
        region: str,
        plan: str,
        os_id: int,
        user_data_b64: str,
        label: str,
        hostname: str,
        enable_ipv6: bool = True,
        sshkey_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /v2/instances → {id, main_ip, status}. main_ip may be '0.0.0.0'
        until Vultr assigns one."""
        body: dict[str, Any] = {
            "region": region,
            "plan": plan,
            "os_id": os_id,
            "user_data": user_data_b64,
            "label": label,
            "hostname": hostname,
            "enable_ipv6": enable_ipv6,
            "backups": "disabled",
        }
        if sshkey_ids:
            body["sshkey_id"] = sshkey_ids
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await self._request(client, "POST", "/instances", json=body)
        if resp.status_code not in (200, 201, 202):
            raise VultrError(f"vultr_create_failed:{resp.status_code}")
        inst = (resp.json() or {}).get("instance") or {}
        return {
            "id": str(inst.get("id")) if inst.get("id") else None,
            "main_ip": inst.get("main_ip") or "0.0.0.0",
            "status": inst.get("status"),
        }

    async def get_instance(self, instance_id: str) -> dict[str, Any]:
        """GET /v2/instances/{id} → full instance dict."""
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await self._request(client, "GET", f"/instances/{instance_id}")
        if resp.status_code != 200:
            raise VultrError(f"vultr_get_instance_failed:{resp.status_code}")
        return (resp.json() or {}).get("instance") or {}

    async def destroy_instance(self, instance_id: str) -> bool:
        """DELETE /v2/instances/{id}. Best-effort: a non-2xx/404 is logged via the
        returned bool (False) rather than raising — used for cost-guard rollback
        and future decommission, where a missing instance is already the goal."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await self._request(
                    client, "DELETE", f"/instances/{instance_id}"
                )
        except VultrError:
            return False
        return resp.status_code in (204, 404)


async def client_for_account(account_id: int) -> VultrClient:
    """Build a VultrClient for an account (resolves + decrypts its key)."""
    key = await asyncio.to_thread(_account_api_key, account_id)
    return VultrClient(key)
