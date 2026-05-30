"""Admin endpoints: Vultr accounts CRUD + node provision-prepare. Wave PROVISION-1 ②.

Mounted with Depends(require_api_key) (see main.py). Consumed by the admin bot
(Промпт ③). Account API keys are Fernet-encrypted at rest; list responses mask
them (never return plaintext).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from orchestrator import vultr
from orchestrator.config import get_config
from orchestrator.crypto import FernetKeyError, decrypt_secret, encrypt_secret, mask_secret
from orchestrator.db import execute, fetch_all, fetch_one
from orchestrator.provision import create_and_provision, create_provision_job, get_provision
from orchestrator.vultr import VultrAccountNotFoundError, VultrError

admin_vultr_router = APIRouter(prefix="/v1/admin")


# ── Vultr accounts CRUD ───────────────────────────────────────────────────────


def _masked_key(api_key_enc: str) -> str:
    try:
        return mask_secret(decrypt_secret(api_key_enc))
    except FernetKeyError:
        return "****"


@admin_vultr_router.get("/vultr-accounts")
async def list_vultr_accounts() -> JSONResponse:
    rows = await asyncio.to_thread(
        fetch_all,
        "select id, label, api_key_enc, enabled, created_at, updated_at "
        "from vultr_accounts order by id",
    )
    accounts = [
        {
            "id": r["id"],
            "label": r["label"],
            "enabled": r["enabled"],
            "key_masked": _masked_key(str(r["api_key_enc"])),
            "created_at": str(r["created_at"]) if r["created_at"] is not None else None,
            "updated_at": str(r["updated_at"]) if r["updated_at"] is not None else None,
        }
        for r in rows
    ]
    return JSONResponse(content={"accounts": accounts})


@admin_vultr_router.post("/vultr-accounts")
async def create_vultr_account(payload: dict[str, Any]) -> JSONResponse:
    label = str(payload.get("label", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    if not label or not api_key:
        raise HTTPException(status_code=400, detail="label_and_api_key_required")
    try:
        enc = await asyncio.to_thread(encrypt_secret, api_key)
    except FernetKeyError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    existing = await asyncio.to_thread(
        fetch_one, "select id from vultr_accounts where label = %s", (label,)
    )
    if existing:
        raise HTTPException(status_code=409, detail="label_already_exists")

    row = await asyncio.to_thread(
        fetch_one,
        "insert into vultr_accounts (label, api_key_enc, enabled) "
        "values (%s, %s, true) returning id, label, enabled",
        (label, enc),
    )
    if row is None:
        raise HTTPException(status_code=500, detail="insert_failed")
    return JSONResponse(
        status_code=201,
        content={
            "id": row["id"],
            "label": row["label"],
            "enabled": row["enabled"],
            "key_masked": mask_secret(api_key),
        },
    )


@admin_vultr_router.patch("/vultr-accounts/{account_id}")
async def update_vultr_account(account_id: int, payload: dict[str, Any]) -> JSONResponse:
    acct = await asyncio.to_thread(
        fetch_one, "select id from vultr_accounts where id = %s", (account_id,)
    )
    if not acct:
        raise HTTPException(status_code=404, detail="vultr_account_not_found")

    sets: list[str] = []
    params: list[Any] = []
    if "label" in payload:
        label = str(payload["label"]).strip()
        if not label:
            raise HTTPException(status_code=400, detail="label_empty")
        sets.append("label = %s")
        params.append(label)
    if "api_key" in payload:
        api_key = str(payload["api_key"]).strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="api_key_empty")
        try:
            enc = await asyncio.to_thread(encrypt_secret, api_key)
        except FernetKeyError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        sets.append("api_key_enc = %s")
        params.append(enc)
    if "enabled" in payload:
        sets.append("enabled = %s")
        params.append(bool(payload["enabled"]))

    if not sets:
        raise HTTPException(status_code=400, detail="no_fields_to_update")

    sets.append("updated_at = now()")
    params.append(account_id)
    try:
        await asyncio.to_thread(
            execute,
            f"update vultr_accounts set {', '.join(sets)} where id = %s",
            tuple(params),
        )
    except Exception as exc:  # unique label violation etc.
        raise HTTPException(status_code=409, detail=f"update_failed:{exc}") from exc
    return JSONResponse(content={"id": account_id, "updated": True})


@admin_vultr_router.delete("/vultr-accounts/{account_id}")
async def disable_vultr_account(account_id: int) -> JSONResponse:
    """Soft-delete: disable the account (keeps nodes' FK + history intact)."""
    acct = await asyncio.to_thread(
        fetch_one, "select id from vultr_accounts where id = %s", (account_id,)
    )
    if not acct:
        raise HTTPException(status_code=404, detail="vultr_account_not_found")
    await asyncio.to_thread(
        execute,
        "update vultr_accounts set enabled = false, updated_at = now() where id = %s",
        (account_id,),
    )
    return JSONResponse(content={"id": account_id, "enabled": False, "disabled": True})


# ── provision-prepare (variant B: NO instance creation) ───────────────────────


@admin_vultr_router.post("/nodes/provision-prepare")
async def provision_prepare(payload: dict[str, Any]) -> JSONResponse:
    try:
        account_id = int(payload["account_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="account_id_required") from exc
    geo = str(payload.get("geo", "")).strip()
    if not geo:
        raise HTTPException(status_code=400, detail="geo_required")
    region = (str(payload["region"]).strip() or None) if payload.get("region") else None
    plan = (str(payload["plan"]).strip() or None) if payload.get("plan") else None
    target_stock = int(payload.get("target_stock") or 4000)

    try:
        result = await asyncio.to_thread(
            create_provision_job,
            account_id=account_id,
            geo=geo,
            region=region,
            plan=plan,
            target_stock=target_stock,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content=result)


@admin_vultr_router.get("/nodes/provision/{job_id}")
async def provision_status(job_id: str) -> JSONResponse:
    row = await asyncio.to_thread(get_provision, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="provision_not_found")
    # created_at/updated_at/finished_at are datetimes — stringify for JSON.
    for k in ("created_at", "updated_at", "finished_at"):
        if row.get(k) is not None:
            row[k] = str(row[k])
    return JSONResponse(content=row)


# ── variant A: region/plan listings + full create-and-provision ───────────────


@admin_vultr_router.get("/vultr/regions")
async def list_vultr_regions(account_id: int) -> JSONResponse:
    """Real Vultr regions for the account's key (city/country/continent + slug)."""
    try:
        client = await vultr.client_for_account(account_id)
    except VultrAccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        regions = await client.list_regions()
    except VultrError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(content={"regions": regions})


@admin_vultr_router.get("/vultr/plans")
async def list_vultr_plans(account_id: int) -> JSONResponse:
    """Node-suitable Vultr plans (>=2 vCPU / >=4 GB), sorted by monthly_cost."""
    try:
        client = await vultr.client_for_account(account_id)
    except VultrAccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        plans = await client.list_plans()
    except VultrError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(content={"plans": plans})


def _normalize_backups(raw: Any) -> str:
    """Normalize the provision-create 'backups' payload (bool | str | None) into
    the Vultr literal 'enabled' / 'disabled'. Default 'disabled'."""
    if isinstance(raw, bool):
        return "enabled" if raw else "disabled"
    if isinstance(raw, str):
        return "enabled" if raw.strip().lower() in ("enabled", "true", "yes", "on", "1") else "disabled"
    return "disabled"


def _count_live_nodes() -> int:
    """Cost-guard tally: every node row that still represents a billed Vultr box
    (anything not explicitly disabled)."""
    row = fetch_one("select count(*) as n from nodes where runtime_status <> 'disabled'")
    return int(row["n"]) if row else 0


@admin_vultr_router.post("/nodes/provision-create")
async def provision_create(payload: dict[str, Any]) -> JSONResponse:
    try:
        account_id = int(payload["account_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="account_id_required") from exc
    region = str(payload.get("region", "")).strip()
    plan = str(payload.get("plan", "")).strip()
    if not region or not plan:
        raise HTTPException(status_code=400, detail="region_and_plan_required")
    geo = str(payload.get("geo", "")).strip()
    if not geo:
        raise HTTPException(status_code=400, detail="geo_required")
    target_stock = int(payload.get("target_stock") or 4000)
    backups = _normalize_backups(payload.get("backups"))

    # COST-GUARD: refuse to spin up another paid box past the configured ceiling.
    max_nodes = get_config().max_nodes
    live = await asyncio.to_thread(_count_live_nodes)
    if live >= max_nodes:
        raise HTTPException(status_code=409, detail=f"node_limit_reached:{live}")

    try:
        result = await create_and_provision(
            account_id=account_id,
            region=region,
            plan=plan,
            geo=geo,
            target_stock=target_stock,
            backups=backups,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except VultrError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return JSONResponse(status_code=201, content=result)
