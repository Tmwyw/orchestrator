"""Node provisioning service (variant B). Wave PROVISION-1 ②.

- render_user_data / build_oneliner: turn the bundled cloud-init template into a
  ready-to-paste Vultr user_data (and an equivalent one-liner for a server that
  already exists), with __ORCH_URL__ / __SECRET__ / __JOB_ID__ substituted.
- create_provision_job: provision-prepare (ЭТАП E) — no instance is created.
- lookup_provision_job / mark_provision_failed / complete_registration: the
  /v1/nodes/register flow (ЭТАП C), all idempotent.

The orchestrator never sees the plaintext secret at rest — only sha256(secret)
in node_provisions.shared_secret_hash.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import uuid
from pathlib import Path
from typing import Any

from orchestrator import vultr
from orchestrator.config import get_config
from orchestrator.db import connect, fetch_one

# proxy_inventory rows in any of these states are stale once a node re-registers
# (it was wiped + reinstalled) — archive them so they leave the live pool.
_PHANTOM_INVENTORY_STATES = (
    "available",
    "sold",
    "allocated_pergb",
    "pending_validation",
    "reserved",
    "expired_grace",
    "invalid",
)

# Defaults for a geo-SKU auto-created at register time (editable afterwards).
_DEFAULT_SKU_KIND = "dualstack"
_DEFAULT_SKU_PROTOCOL = "socks5"
_DEFAULT_SKU_DURATION_DAYS = 30
_DEFAULT_SKU_PRICE = "0.14"
_DEFAULT_TARGET_STOCK = 4000


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def node_id_for_ip(ip: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"http://{ip}:8085").hex


# ── user_data rendering ───────────────────────────────────────────────────────


def render_user_data(*, orch_url: str, secret: str, job_id: str) -> str:
    """Read the bundled cloud-init template, substitute the 3 markers."""
    path = Path(get_config().cloud_init_template_path)
    template = path.read_text(encoding="utf-8")
    return (
        template.replace("__ORCH_URL__", orch_url.rstrip("/"))
        .replace("__SECRET__", secret)
        .replace("__JOB_ID__", job_id)
    )


def build_oneliner(user_data: str) -> str:
    """Self-contained one-liner for an already-running server (no drift: it runs
    the exact same rendered script, base64-packed)."""
    packed = base64.b64encode(user_data.encode("utf-8")).decode("ascii")
    return f"echo {packed} | base64 -d | sudo bash"


# ── provision-prepare (ЭТАП E) ────────────────────────────────────────────────


def create_provision_job(
    *,
    account_id: int,
    geo: str,
    region: str | None,
    plan: str | None,
    target_stock: int,
) -> dict[str, Any]:
    """Insert a node_provisions row + return the one-time secret (plain, once).

    Variant B: does NOT create a Vultr instance.
    """
    cfg = get_config()
    if not cfg.orch_base_url:
        raise RuntimeError("orchestrator_base_url_not_configured")

    account = fetch_one(
        "select id, enabled from vultr_accounts where id = %s", (account_id,)
    )
    if not account:
        raise LookupError(f"vultr_account_not_found:{account_id}")
    if not account.get("enabled"):
        raise PermissionError(f"vultr_account_disabled:{account_id}")

    job_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(24)
    secret_hash = hash_secret(secret)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into node_provisions
              (job_id, account_id, geo, region, plan, target_stock,
               shared_secret_hash, status)
            values (%s, %s, %s, %s, %s, %s, %s, 'installing')
            """,
            (job_id, account_id, geo, region, plan, target_stock, secret_hash),
        )

    user_data = render_user_data(orch_url=cfg.orch_base_url, secret=secret, job_id=job_id)
    return {
        "job_id": job_id,
        "secret": secret,  # plaintext — shown ONCE
        "cloud_init_user_data": user_data,
        "oneliner_command": build_oneliner(user_data),
    }


# ── provision-create (variant A: orchestrator creates the Vultr box) ──────────


def _insert_installing_job(
    *,
    job_id: str,
    account_id: int,
    geo: str,
    region: str | None,
    plan: str | None,
    target_stock: int,
    secret_hash: str,
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into node_provisions
              (job_id, account_id, geo, region, plan, target_stock,
               shared_secret_hash, status)
            values (%s, %s, %s, %s, %s, %s, %s, 'installing')
            """,
            (job_id, account_id, geo, region, plan, target_stock, secret_hash),
        )


def _mark_create_failed(job_id: str) -> None:
    """create_instance blew up after the job row landed — never leave it hanging
    in 'installing' (no box was created)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            update node_provisions
               set status = 'failed',
                   error = 'vultr_create_failed',
                   updated_at = now(),
                   finished_at = now()
             where job_id = %s
            """,
            (job_id,),
        )


def _record_created_instance(*, job_id: str, instance_id: str | None, ip: str | None) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            update node_provisions
               set vultr_instance_id = %s,
                   ip = %s,
                   updated_at = now()
             where job_id = %s
            """,
            (instance_id, ip, job_id),
        )


async def create_and_provision(
    *,
    account_id: int,
    region: str,
    plan: str,
    geo: str,
    target_stock: int,
    backups: str = "disabled",
) -> dict[str, Any]:
    """Variant A: create the Vultr instance (cloud-init injected via user_data),
    then let the node self-register via POST /v1/nodes/register.

    Inserts an 'installing' node_provisions row BEFORE the API call so a partial
    failure is always recorded; if create_instance raises, flips that row to
    'failed' (error='vultr_create_failed') and re-raises — no box exists in that
    branch (failure happens at/before the POST), so there is nothing to destroy.
    """
    cfg = get_config()
    if not cfg.orch_base_url:
        raise RuntimeError("orchestrator_base_url_not_configured")

    account = await asyncio.to_thread(
        fetch_one, "select id, enabled from vultr_accounts where id = %s", (account_id,)
    )
    if not account:
        raise LookupError(f"vultr_account_not_found:{account_id}")
    if not account.get("enabled"):
        raise PermissionError(f"vultr_account_disabled:{account_id}")

    job_id = uuid.uuid4().hex
    secret = secrets.token_urlsafe(24)
    secret_hash = hash_secret(secret)
    await asyncio.to_thread(
        _insert_installing_job,
        job_id=job_id,
        account_id=account_id,
        geo=geo,
        region=region,
        plan=plan,
        target_stock=target_stock,
        secret_hash=secret_hash,
    )

    user_data = render_user_data(orch_url=cfg.orch_base_url, secret=secret, job_id=job_id)
    user_data_b64 = base64.b64encode(user_data.encode("utf-8")).decode("ascii")

    # Operator convention: every node's Vultr hostname + label is "NETRUN"
    # (matches the 7 manual nodes). Internal node.name (node-<geo>-<id8>, set at
    # /register) is what distinguishes them in the bot panel.
    label = "NETRUN"
    hostname = "NETRUN"

    try:
        client = await vultr.client_for_account(account_id)
        os_id = await client.resolve_ubuntu_2404_os_id()
        inst = await client.create_instance(
            region=region,
            plan=plan,
            os_id=os_id,
            user_data_b64=user_data_b64,
            label=label,
            hostname=hostname,
            backups=backups,
        )
    except Exception:
        await asyncio.to_thread(_mark_create_failed, job_id)
        raise

    main_ip = str(inst.get("main_ip") or "0.0.0.0")
    ip = None if main_ip == "0.0.0.0" else main_ip
    instance_id = inst.get("id")
    await asyncio.to_thread(
        _record_created_instance, job_id=job_id, instance_id=instance_id, ip=ip
    )

    return {
        "job_id": job_id,
        "vultr_instance_id": instance_id,
        "status": "installing",
        "main_ip": main_ip,
    }


def get_provision(job_id: str) -> dict[str, Any] | None:
    return fetch_one(
        """
        select job_id, account_id, geo, region, plan, target_stock, status,
               ip, vultr_instance_id, install_log_tail, error,
               created_at, updated_at, finished_at
        from node_provisions where job_id = %s
        """,
        (job_id,),
    )


# ── /v1/nodes/register (ЭТАП C) ───────────────────────────────────────────────


def lookup_provision_job(secret: str) -> dict[str, Any] | None:
    """Find the active provision job whose secret hashes to ``secret``.

    Accepts 'installing' (first call) and 'registered' (idempotent re-call).
    """
    return fetch_one(
        """
        select job_id, account_id, geo, target_stock, status
        from node_provisions
        where shared_secret_hash = %s and status in ('installing','registered')
        order by created_at desc
        limit 1
        """,
        (hash_secret(secret),),
    )


def mark_provision_failed(
    *, job_id: str, exit_code: int, log_tail: str, ip: str
) -> None:
    """Step 1: install_result.ok=false → status='failed' (never hangs)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            update node_provisions
               set status = 'failed',
                   ip = %s,
                   install_log_tail = %s,
                   error = %s,
                   updated_at = now(),
                   finished_at = now()
             where job_id = %s
            """,
            (ip, log_tail, f"install_failed:exit={exit_code}", job_id),
        )


def complete_registration(
    *,
    job: dict[str, Any],
    ip: str,
    vultr_instance_id: str | None,
    log_tail: str,
) -> dict[str, Any]:
    """Steps 3-6 in one transaction. Idempotent (all upserts / ON CONFLICT)."""
    job_id = str(job["job_id"])
    account_id = job.get("account_id")
    geo = (str(job.get("geo") or "")).strip()
    target_stock = int(job.get("target_stock") or _DEFAULT_TARGET_STOCK)
    node_id = node_id_for_ip(ip)
    url = f"http://{ip}:8085"
    capacity = target_stock  # node's intended contribution to the geo pool (editable)

    with connect() as conn, conn.cursor() as cur:
        # STEP 3: upsert the node (active + tied to its Vultr account/instance).
        cur.execute(
            """
            insert into nodes
              (id, name, url, geo, status, runtime_status, capacity,
               vultr_account, vultr_instance_id, last_health_check)
            values (%s, %s, %s, %s, 'ready', 'active', %s, %s, %s, now())
            on conflict (id) do update set
              url = excluded.url,
              geo = excluded.geo,
              status = 'ready',
              runtime_status = 'active',
              capacity = excluded.capacity,
              vultr_account = excluded.vultr_account,
              vultr_instance_id = coalesce(excluded.vultr_instance_id, nodes.vultr_instance_id),
              last_health_check = now(),
              heartbeat_failures = 0,
              updated_at = now()
            """,
            (
                node_id,
                f"node-{geo.lower()}-{node_id[:8]}" if geo else f"node-{node_id[:8]}",
                url,
                geo,
                capacity,
                account_id,
                vultr_instance_id,
            ),
        )

        # STEP 4: bind to the geo's active SKUs; create a default SKU if none.
        sku_ids: list[int] = []
        if geo:
            cur.execute(
                "select id from skus where is_active = true and geo_code = %s", (geo,)
            )
            sku_ids = [int(r["id"]) for r in cur.fetchall()]
            if not sku_ids:
                cur.execute(
                    """
                    insert into skus
                      (code, product_kind, geo_code, protocol, duration_days,
                       price_per_piece, target_stock, refill_batch_size, is_active)
                    values (%s, %s, %s, %s, %s, %s, %s, 500, true)
                    on conflict (code) do update set is_active = true, updated_at = now()
                    returning id
                    """,
                    (
                        f"{_DEFAULT_SKU_KIND}_{geo.lower()}",
                        _DEFAULT_SKU_KIND,
                        geo,
                        _DEFAULT_SKU_PROTOCOL,
                        _DEFAULT_SKU_DURATION_DAYS,
                        _DEFAULT_SKU_PRICE,
                        target_stock,
                    ),
                )
                created = cur.fetchone()
                if created:
                    sku_ids = [int(created["id"])]

            for sku_id in sku_ids:
                cur.execute(
                    """
                    insert into sku_node_bindings
                      (sku_id, node_id, weight, max_batch_size, is_active, target_stock)
                    values (%s, %s, 100, 1500, true, %s)
                    on conflict (sku_id, node_id) do update set
                      is_active = true,
                      target_stock = excluded.target_stock,
                      updated_at = now()
                    """,
                    (sku_id, node_id, target_stock),
                )

        # STEP 5: archive phantom inventory left over from a prior install.
        cur.execute(
            """
            update proxy_inventory
               set status = 'archived'
             where node_id = %s and status = ANY(%s)
            """,
            (node_id, list(_PHANTOM_INVENTORY_STATES)),
        )

        # STEP 6: mark the provision registered.
        cur.execute(
            """
            update node_provisions
               set status = 'registered',
                   ip = %s,
                   vultr_instance_id = coalesce(%s, vultr_instance_id),
                   install_log_tail = %s,
                   error = null,
                   updated_at = now(),
                   finished_at = now()
             where job_id = %s
            """,
            (ip, vultr_instance_id, log_tail, job_id),
        )

    return {
        "node_id": node_id,
        "geo": geo,
        "bound_skus": sku_ids,
        "vultr_instance_id": vultr_instance_id,
    }
