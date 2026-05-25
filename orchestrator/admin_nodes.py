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

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

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
    """Reboot the node's Vultr instance (instance id looked up by the node IP)."""
    node = await asyncio.to_thread(fetch_one, "select url from nodes where id = %s", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="node_not_found")
    ip = _ip_from_url(str(node.get("url") or ""))
    if not ip:
        raise HTTPException(status_code=400, detail="cannot_parse_node_ip")
    key = _vultr_api_key()
    if not key:
        raise HTTPException(status_code=500, detail="vultr_api_key_unavailable")

    headers = {"Authorization": f"Bearer {key}"}
    iid: str | None = None
    cursor = ""
    async with httpx.AsyncClient(timeout=20) as client:
        for _ in range(10):  # paginate defensively
            params: dict[str, Any] = {"per_page": 100}
            if cursor:
                params["cursor"] = cursor
            r = await client.get(
                "https://api.vultr.com/v2/instances", headers=headers, params=params
            )
            if r.status_code != 200:
                raise HTTPException(status_code=502, detail=f"vultr_list_failed:{r.status_code}")
            data = r.json()
            for inst in data.get("instances", []):
                if inst.get("main_ip") == ip:
                    iid = inst.get("id")
                    break
            if iid:
                break
            cursor = ((data.get("meta") or {}).get("links") or {}).get("next") or ""
            if not cursor:
                break
        if not iid:
            raise HTTPException(status_code=404, detail=f"vultr_instance_not_found_for_ip:{ip}")
        rb = await client.post(
            f"https://api.vultr.com/v2/instances/{iid}/reboot", headers=headers
        )
        if rb.status_code not in (202, 204):
            raise HTTPException(status_code=502, detail=f"vultr_reboot_failed:{rb.status_code}")
    return JSONResponse(
        content={"id": node_id, "ip": ip, "vultr_instance_id": iid, "rebooted": True}
    )
