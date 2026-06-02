"""Admin node-management endpoints (Wave NODE-MGMT / Phase 2).

Lets the admin bot list nodes, enable/disable them (runtime_status), recover a
node stuck in 'degraded' back to 'active', and reboot the node's Vultr instance.

- runtime_status gates refill (refill.py) and reservation (allocator.py): only
  'active' (or 'degraded' when allow_degraded) bindings are used. Setting
  'disabled' takes a node out of rotation without touching its inventory.
- Reboot looks up the Vultr instance id by the node's IP (parsed from nodes.url)
  via the Vultr API; the API key is read from VULTR_API_KEY env or the
  vultr_watchdog.env file the watchdog already uses.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from orchestrator import vultr
from orchestrator.crypto import FernetKeyError
from orchestrator.db import execute, fetch_all, fetch_one

admin_nodes_router = APIRouter(prefix="/v1/admin")

# CHECK constraint on nodes.runtime_status (migration 003_extend_nodes.sql).
_VALID_RUNTIME_STATUS = {"active", "degraded", "offline", "disabled"}
# Admin may only set these from the bot (degraded is system-set by traffic_poll).
_SETTABLE_RUNTIME_STATUS = {"active", "disabled", "offline"}
_VULTR_ENV = "/opt/netrun-orchestrator/vultr_watchdog.env"


def _vultr_api_key() -> str:
    """Vultr API key: VULTR_API_KEY env, else parsed from vultr_watchdog.env."""
    key = os.getenv("VULTR_API_KEY", "").strip()
    if key:
        return key
    try:
        for raw in Path(_VULTR_ENV).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("VULTR_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _ip_from_url(url: str) -> str | None:
    m = re.search(r"https?://([^:/]+)", url or "")
    return m.group(1) if m else None


class NodeRebootError(RuntimeError):
    """Reboot could not be performed. ``detail`` mirrors the HTTP detail
    strings the endpoint historically returned so callers (endpoint +
    egress watchdog) can log/translate uniformly."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def reboot_node_internal(node_id: str) -> dict[str, Any]:
    """Reboot the node's Vultr instance using ITS account's key.

    Wave WATCHDOG-EGRESS-CHECK extracted this from :func:`reboot_node` so
    the egress watchdog can trigger a reboot DIRECTLY (no HTTP self-call).
    Raises :class:`NodeRebootError` on any failure (the endpoint maps it to
    the same HTTP statuses it used before; the watchdog catches + logs).

    Per-account: a node tied to ``vultr_account`` is rebooted with that
    account's decrypted key (a Vultr key only sees its own instances).
    Legacy nodes with no account fall back to ``VULTR_API_KEY`` env /
    ``vultr_watchdog.env``. The instance id comes from
    ``nodes.vultr_instance_id`` when present, else looked up by node IP.
    """
    node = await asyncio.to_thread(
        fetch_one,
        "select url, vultr_account, vultr_instance_id from nodes where id = %s",
        (node_id,),
    )
    if not node:
        raise NodeRebootError("node_not_found")
    ip = _ip_from_url(str(node.get("url") or ""))
    iid: str | None = (str(node.get("vultr_instance_id") or "").strip()) or None
    account_id = node.get("vultr_account")

    if account_id is not None:
        try:
            client = await vultr.client_for_account(int(account_id))
        except (vultr.VultrError, FernetKeyError) as exc:
            raise NodeRebootError(f"vultr_account_key_error:{exc}") from exc
    else:
        key = _vultr_api_key()
        if not key:
            raise NodeRebootError("vultr_api_key_unavailable")
        client = vultr.VultrClient(key)

    if not iid:
        if not ip:
            raise NodeRebootError("cannot_parse_node_ip")
        try:
            iid = await client.find_instance_id_by_main_ip(ip)
        except vultr.VultrError as exc:
            raise NodeRebootError(f"vultr_list_failed:{exc}") from exc
        if not iid:
            raise NodeRebootError(f"vultr_instance_not_found_for_ip:{ip}")

    try:
        await client.reboot(iid)
    except vultr.VultrError as exc:
        raise NodeRebootError(f"vultr_reboot_failed:{exc}") from exc
    return {"id": node_id, "ip": ip, "vultr_instance_id": iid, "rebooted": True}


@admin_nodes_router.get("/nodes")
async def list_nodes_admin() -> JSONResponse:
    """Nodes with runtime_status + available proxy count (for the bot node menu)."""
    rows = await asyncio.to_thread(
        fetch_all,
        """
        select n.id, n.name, n.geo, n.url, n.status, n.runtime_status,
               n.capacity, n.last_heartbeat_at,
               coalesce(av.available, 0)::int as available
        from nodes n
        left join (
            select node_id, count(*) as available
            from proxy_inventory
            where status = 'available'
            group by node_id
        ) av on av.node_id = n.id
        order by n.geo, n.name
        """,
    )
    # last_heartbeat_at is a datetime — stringify so the stdlib JSON encoder
    # in JSONResponse can serialize it (mirrors provision_status).
    for r in rows:
        if r.get("last_heartbeat_at") is not None:
            r["last_heartbeat_at"] = str(r["last_heartbeat_at"])
    return JSONResponse(content={"nodes": rows})


@admin_nodes_router.patch("/nodes/{node_id}")
async def set_node_runtime_status(node_id: str, payload: dict[str, Any]) -> JSONResponse:
    """Enable/disable a node, or recover degraded->active. Body: {runtime_status}."""
    new_status = str(payload.get("runtime_status", "")).strip().lower()
    if new_status not in _SETTABLE_RUNTIME_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"runtime_status must be one of {sorted(_SETTABLE_RUNTIME_STATUS)}",
        )
    node = await asyncio.to_thread(
        fetch_one, "select id, runtime_status from nodes where id = %s", (node_id,)
    )
    if not node:
        raise HTTPException(status_code=404, detail="node_not_found")
    await asyncio.to_thread(
        execute,
        "update nodes set runtime_status = %s, heartbeat_failures = 0, updated_at = now() "
        "where id = %s",
        (new_status, node_id),
    )
    return JSONResponse(
        content={
            "id": node_id,
            "runtime_status": new_status,
            "previous": node.get("runtime_status"),
        }
    )


@admin_nodes_router.post("/nodes/{node_id}/reboot")
async def reboot_node(node_id: str) -> JSONResponse:
    """Reboot the node's Vultr instance using ITS account's key (Wave PROVISION-1 ②).

    Per-account: a node tied to vultr_account is rebooted with that account's
    decrypted key (a Vultr key only sees its own instances). Legacy nodes with
    no account fall back to the single VULTR_API_KEY env / watchdog.env file.
    The instance id is taken from nodes.vultr_instance_id when present, else
    looked up by the node IP.

    Wave WATCHDOG-EGRESS-CHECK: the body moved to :func:`reboot_node_internal`
    (shared with the egress watchdog); this endpoint just maps its
    ``NodeRebootError.detail`` back to the historical HTTP statuses.
    """
    try:
        result = await reboot_node_internal(node_id)
    except NodeRebootError as exc:
        raise HTTPException(status_code=_reboot_error_status(exc.detail), detail=exc.detail) from exc
    return JSONResponse(content=result)


def _reboot_error_status(detail: str) -> int:
    """Preserve the original per-failure HTTP status codes."""
    if detail == "node_not_found" or detail.startswith("vultr_instance_not_found_for_ip"):
        return 404
    if detail == "cannot_parse_node_ip":
        return 400
    if detail == "vultr_api_key_unavailable":
        return 500
    return 502  # vultr_account_key_error / vultr_list_failed / vultr_reboot_failed
