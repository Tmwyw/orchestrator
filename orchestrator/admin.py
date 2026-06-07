"""Admin endpoints: stats, orders search, archive export, pergb force-poll."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from orchestrator import node_client
from orchestrator.api_schemas import (
    AdminChangeExpiryRequest,
    AdminChangeExpiryResponse,
    AdminSetQuotaRequest,
    AdminSetQuotaResponse,
    AdminTrafficPollResponse,
    AdminUserTrafficRequest,
    AdminUserTrafficResponse,
    ArchiveExportItem,
    ArchiveExportResponse,
    OrderListItem,
    OrdersListResponse,
    PergbStatsSubsection,
    PergbTopSku,
    StatsInventoryRow,
    StatsNodes,
    StatsResponse,
    StatsSales,
)
from orchestrator.db import connect, fetch_all, fetch_one
from orchestrator.logging_setup import get_logger
from orchestrator.traffic_poll import TrafficPollService

# Mypy on py310 doesn't recognise ``datetime.UTC`` (added in 3.11);
# keep the alias compatible with both runtime + type-check.
UTC = timezone.utc

logger = get_logger("netrun-orchestrator-admin")

admin_router = APIRouter(prefix="/v1/admin")

# Module-level singleton — the lock inside is per-process. The scheduler
# unit lives in a different process and has its own lock; cross-process
# overlap is rare (60s cadence) and the DB writes are atomic per row.
_traffic_poll_service = TrafficPollService()


@admin_router.get("/stats")
async def stats(range_days: int = 7) -> JSONResponse:
    """Sales / inventory / nodes summary for last N days."""
    sales_row = await asyncio.to_thread(
        fetch_one,
        """
        select count(*)::int as orders,
               coalesce(sum(allocated_count), 0)::int as proxies,
               coalesce(sum(price_amount), 0) as revenue
        from orders
        where status = 'committed'
          and committed_at > now() - (%s || ' days')::interval
        """,
        (range_days,),
    )
    inventory_rows = await asyncio.to_thread(
        fetch_all,
        """
        select s.code, i.status, count(*)::int as n
        from proxy_inventory i join skus s on s.id = i.sku_id
        group by s.code, i.status
        order by s.code, i.status
        """,
    )
    nodes_row = await asyncio.to_thread(
        fetch_one,
        """
        select count(*) filter (where status = 'ready')::int as ready,
               count(*)::int as total
        from nodes
        """,
    )
    pergb_section = await asyncio.to_thread(_fetch_pergb_stats)
    response = StatsResponse(
        sales=StatsSales(**(sales_row or {})),
        inventory=[StatsInventoryRow(**r) for r in inventory_rows],
        nodes=StatsNodes(**(nodes_row or {})),
        pergb=pergb_section,
    )
    return JSONResponse(content=response.model_dump(mode="json"))


def _fetch_pergb_stats() -> PergbStatsSubsection:
    """Pay-per-GB summary block on /v1/admin/stats (B-8.3).

    Three small SELECTs — count by status, 7-day bytes from samples, top 5
    SKUs by 7-day revenue. ``traffic_accounts`` may be empty in fresh
    environments; defaults handle that cleanly.
    """
    counts_row = fetch_one(
        """
        select
          count(*) filter (where status = 'active')::int   as active_accounts,
          count(*) filter (where status = 'depleted')::int as depleted_accounts,
          count(*) filter (where status = 'expired')::int  as expired_accounts
        from traffic_accounts
        """,
    ) or {"active_accounts": 0, "depleted_accounts": 0, "expired_accounts": 0}

    bytes_row = fetch_one(
        """
        select coalesce(sum(bytes_in_delta + bytes_out_delta), 0)::bigint as bytes_7d
        from traffic_samples
        where collected_at > now() - interval '7 days'
        """,
    ) or {"bytes_7d": 0}

    top_rows = fetch_all(
        """
        select s.code as sku_code,
               coalesce(sum(o.price_amount), 0) as revenue,
               count(distinct t.id)::int as accounts
        from orders o
        join skus s on s.id = o.sku_id
        -- Wave PERGB-POOL-1: per-USER pool — distinct accounts ≈ distinct
        -- paying users per SKU (join via owner, order_id is canonical-only now).
        left join traffic_accounts t on t.user_id = o.user_id
        where s.product_kind = 'datacenter_pergb'
          and o.created_at > now() - interval '7 days'
        group by s.code
        order by revenue desc
        limit 5
        """,
    )
    return PergbStatsSubsection(
        active_accounts=int(counts_row.get("active_accounts") or 0),
        depleted_accounts=int(counts_row.get("depleted_accounts") or 0),
        expired_accounts=int(counts_row.get("expired_accounts") or 0),
        bytes_consumed_7d=int(bytes_row.get("bytes_7d") or 0),
        top_skus_by_revenue_7d=[
            PergbTopSku(
                sku_code=str(r["sku_code"]),
                revenue=r["revenue"],
                accounts=int(r["accounts"]),
            )
            for r in top_rows
        ],
    )


@admin_router.get("/orders")
async def orders_search(
    user_id: int | None = None,
    status: str | None = None,
    limit: int = 100,
) -> JSONResponse:
    """Search orders by user_id and/or status."""
    where: list[str] = ["1=1"]
    params: list[Any] = []
    if user_id is not None:
        where.append("user_id = %s")
        params.append(user_id)
    if status is not None:
        where.append("status = %s")
        params.append(status)
    params.append(min(limit, 1000))
    rows = await asyncio.to_thread(
        fetch_all,
        f"""
        select order_ref, user_id, sku_id, status, requested_count, allocated_count,
               reserved_at, expires_at, committed_at, proxies_expires_at
        from orders
        where {" and ".join(where)}
        order by created_at desc
        limit %s
        """,
        tuple(params),
    )
    response = OrdersListResponse(
        items=[OrderListItem(**r) for r in rows],
        count=len(rows),
    )
    return JSONResponse(content=response.model_dump(mode="json"))


@admin_router.get("/archive")
async def archive_export(
    from_date: str,
    to_date: str,
    geo: str | None = None,
) -> JSONResponse:
    """Export archived proxies in JSON for accounting."""
    where = ["i.status = 'archived'", "i.archived_at between %s and %s"]
    params: list[Any] = [from_date, to_date]
    if geo:
        where.append("s.geo_code = %s")
        params.append(geo)
    rows = await asyncio.to_thread(
        fetch_all,
        f"""
        select i.id, s.code as sku_code, i.host, i.port, i.login, i.password,
               i.geo_country, i.archived_at, i.order_id
        from proxy_inventory i join skus s on s.id = i.sku_id
        where {" and ".join(where)}
        order by i.archived_at asc
        limit 10000
        """,
        tuple(params),
    )
    response = ArchiveExportResponse(
        items=[ArchiveExportItem(**r) for r in rows],
        count=len(rows),
        **{"from": from_date, "to": to_date},
    )
    return JSONResponse(content=response.model_dump(mode="json", by_alias=True))


# === Pay-per-GB admin force-poll (Wave B-8.3) ===


@admin_router.post("/traffic/poll")
async def force_poll(
    node_id: str | None = None,
    account_id: int | None = None,
) -> JSONResponse:
    """Synchronous force-poll over active pergb traffic accounts.

    Useful for testing depletion → disable transitions and topup_pergb
    reactivation without waiting for the 60s scheduler cadence. Optional
    ``node_id`` / ``account_id`` query params scope the cycle to a single
    node or single account; with neither, runs a full cycle (same shape
    as the scheduler tick).
    """
    counters = await asyncio.to_thread(
        _traffic_poll_service.run_once,
        node_id_filter=node_id,
        account_id_filter=account_id,
    )
    response = AdminTrafficPollResponse(
        accounts_polled=counters.accounts_polled,
        nodes_polled=counters.nodes_polled,
        bytes_observed_total=counters.bytes_observed_total,
        counter_resets_detected=counters.counter_resets_detected,
        accounts_marked_depleted=counters.accounts_depleted,
    )
    return JSONResponse(content=response.model_dump(mode="json"))


# === Wave PER-USER-TOOLS-1: per-user admin tools ===
#
# SET-traffic-quota and change-order-expiry. Mirror the legacy
# panel/proxy_api_v2 admin verbs we relied on in NETRUN, scoped to
# our domain (orchestrator owns proxies + traffic; bot is just the
# UI). Both endpoints are admin-only (mounted under admin_router
# which has require_api_key globally) and operate atomically.


@admin_router.patch("/orders/{order_ref}/quota")
async def admin_set_quota(order_ref: str, payload: AdminSetQuotaRequest) -> JSONResponse:
    """SET (not topup-add) the traffic quota of a pay-per-GB order.

    Semantics:
    * Resolves ``traffic_accounts`` via ``orders.id`` (404 if no
      pergb account is bound to ``order_ref``).
    * Replaces ``bytes_quota`` with ``round(gb_amount * 1024**3)``.
      ``bytes_used`` is preserved untouched.
    * Recomputes ``status``: ``active`` when new quota > used,
      ``depleted`` otherwise. ``archived`` / ``expired`` accounts are
      refused with 409 (we don't resurrect closed accounts).
    """
    bytes_quota = round(payload.gb_amount * (1024**3))
    result = await asyncio.to_thread(_sync_set_quota, order_ref, bytes_quota)
    if result["error"] == "not_found":
        raise HTTPException(status_code=404, detail="pergb traffic account not found")
    if result["error"] == "closed":
        raise HTTPException(
            status_code=409, detail=f"traffic account status={result['status']} — refusing to mutate"
        )
    response = AdminSetQuotaResponse(
        order_ref=order_ref,
        bytes_quota=result["bytes_quota"],
        bytes_used=result["bytes_used"],
        bytes_remaining=max(0, result["bytes_quota"] - result["bytes_used"]),
        status=result["status"],
        expires_at=result["expires_at"],
    )
    return JSONResponse(content=response.model_dump(mode="json"))


def _sync_set_quota(order_ref: str, bytes_quota: int) -> dict[str, Any]:
    """Atomic SET — single UPDATE recomputes status.

    Returns a dict with ``error`` field for the handler to map to HTTP
    status codes; on success ``error`` is empty string and the rest of
    the row carries the post-update state."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select ta.id, ta.bytes_used, ta.status, ta.expires_at
              from traffic_accounts ta
              -- Wave PERGB-POOL-1: per-USER pool — resolve via order owner,
              -- not order_id, so any of the user's order_refs hits the pool.
              join orders o on o.user_id = ta.user_id
             where o.order_ref = %s
             for update of ta
            """,
            (order_ref,),
        )
        row = cur.fetchone()
        if row is None:
            return {"error": "not_found"}
        if row["status"] in ("archived", "expired"):
            return {"error": "closed", "status": row["status"]}
        new_status = "active" if bytes_quota > row["bytes_used"] else "depleted"
        cur.execute(
            """
            update traffic_accounts
               set bytes_quota = %s,
                   status      = %s,
                   updated_at  = now()
             where id = %s
             returning bytes_quota, bytes_used, status, expires_at
            """,
            (bytes_quota, new_status, row["id"]),
        )
        updated = cur.fetchone()
        assert updated is not None  # UPDATE RETURNING on locked row
        return {
            "error": "",
            "bytes_quota": updated["bytes_quota"],
            "bytes_used": updated["bytes_used"],
            "status": updated["status"],
            "expires_at": updated["expires_at"],
        }


# ── Wave PERGB-POOL-1: per-USER GB pool ops (set/add/gift/subtract) ──

_DEFAULT_GIFT_DURATION_DAYS = 30


@admin_router.patch("/users/{user_id}/traffic")
async def admin_set_user_traffic(
    user_id: int, payload: AdminUserTrafficRequest
) -> JSONResponse:
    """SET / ADD / GIFT / SUBTRACT GB on the user's ONE pool (addressed by
    user_id — no order picker). bytes_used preserved; status recomputes and
    the pool's ports are enabled/disabled on an active↔depleted transition
    IMMEDIATELY (not waiting for the watchdog)."""
    delta_bytes = round(payload.gb_amount * (1024**3))
    result = await asyncio.to_thread(
        _sync_apply_user_traffic_op, user_id, payload.op, delta_bytes
    )
    if result["error"] == "not_found":
        raise HTTPException(status_code=404, detail="user has no pergb traffic pool")
    if result["error"] == "closed":
        raise HTTPException(
            status_code=409,
            detail=f"traffic pool status={result['status']} — refusing to mutate",
        )
    # Port fan-out on transition: enable on →active, disable on →depleted.
    if result["old_status"] != result["new_status"]:
        await asyncio.to_thread(
            _fan_out_account_ports,
            int(result["account_id"]),
            enable=(result["new_status"] == "active"),
        )
    response = AdminUserTrafficResponse(
        user_id=user_id,
        bytes_quota=result["bytes_quota"],
        bytes_used=result["bytes_used"],
        bytes_remaining=max(0, result["bytes_quota"] - result["bytes_used"]),
        status=result["new_status"],
        expires_at=result["expires_at"],
    )
    return JSONResponse(content=response.model_dump(mode="json"))


def _sync_apply_user_traffic_op(user_id: int, op: str, delta_bytes: int) -> dict[str, Any]:
    """Apply a set/add/gift/subtract op to the user's single GB pool.

    Creates the pool on set/add/gift when the user has none (default
    30-day window — a gift to a never-bought user still works); subtract on
    a missing pool returns ``not_found``. archived/expired pools are refused
    (409) — those are revived by a re-purchase, not an admin tweak. Returns
    pre/post status so the caller fans out enable/disable on a transition.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select id, bytes_quota, bytes_used, status from traffic_accounts "
            "where user_id = %s for update",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            if op == "subtract":
                return {"error": "not_found"}
            cur.execute(
                """
                insert into traffic_accounts
                  (user_id, order_id, inventory_id, bytes_quota, bytes_used,
                   status, expires_at)
                values (%s, NULL, NULL, %s, 0, 'active',
                        now() + (%s || ' days')::interval)
                returning id, bytes_quota, bytes_used, status, expires_at
                """,
                (user_id, delta_bytes, _DEFAULT_GIFT_DURATION_DAYS),
            )
            created = cur.fetchone()
            assert created is not None
            return {
                "error": "",
                "account_id": int(created["id"]),
                "old_status": "active",
                "new_status": str(created["status"]),
                "bytes_quota": int(created["bytes_quota"]),
                "bytes_used": int(created["bytes_used"]),
                "expires_at": created["expires_at"],
            }
        if str(row["status"]) in ("archived", "expired"):
            return {"error": "closed", "status": str(row["status"])}
        old_status = str(row["status"])
        used = int(row["bytes_used"])
        if op == "set":
            new_quota = delta_bytes
        elif op in ("add", "gift"):
            new_quota = int(row["bytes_quota"]) + delta_bytes
        else:  # subtract
            new_quota = max(0, int(row["bytes_quota"]) - delta_bytes)
        new_status = "active" if new_quota > used else "depleted"
        cur.execute(
            """
            update traffic_accounts
               set bytes_quota = %s,
                   status = %s,
                   depleted_at = case when %s = 'depleted' then now() else null end,
                   updated_at = now()
             where id = %s
            returning bytes_quota, bytes_used, expires_at
            """,
            (new_quota, new_status, new_status, int(row["id"])),
        )
        updated = cur.fetchone()
        assert updated is not None
        return {
            "error": "",
            "account_id": int(row["id"]),
            "old_status": old_status,
            "new_status": new_status,
            "bytes_quota": int(updated["bytes_quota"]),
            "bytes_used": int(updated["bytes_used"]),
            "expires_at": updated["expires_at"],
        }


def _fan_out_account_ports(account_id: int, *, enable: bool) -> None:
    """Best-effort post_enable/post_disable on every port linked to the pool.
    Mirrors traffic_poll's fan-out. ``node_blocked`` is set so the watchdog
    reconciles any port that didn't ack: enable → blocked iff some failed
    (retry-unblock); disable → blocked iff all acked (else retry-block)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select i.port, n.url as node_url, n.api_key as node_api_key
              from proxy_inventory i
              join nodes n on n.id = i.node_id
             where i.traffic_account_id = %s
            """,
            (account_id,),
        )
        ports = list(cur.fetchall())
    all_ok = bool(ports)
    for p in ports:
        node_url = str(p["node_url"])
        api_key = str(p["node_api_key"]) if p.get("node_api_key") else None
        port = int(p["port"])
        try:
            if enable:
                node_client.post_enable(node_url, api_key, port)
            else:
                node_client.post_disable(node_url, api_key, port)
        except node_client.NodeAgentError as exc:
            all_ok = False
            logger.warning(
                "admin_traffic_fanout_failed",
                account_id=account_id,
                port=port,
                enable=enable,
                error=str(exc),
            )
    node_blocked = (not all_ok) if enable else all_ok
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "update traffic_accounts set node_blocked = %s, updated_at = now() where id = %s",
            (node_blocked, account_id),
        )


@admin_router.patch("/orders/{order_ref}/expiry")
async def admin_change_expiry(order_ref: str, payload: AdminChangeExpiryRequest) -> JSONResponse:
    """Change the expiry of an order in either direction.

    * ``add`` — new = current + days (NULL current → now() + days).
    * ``set`` — new = now() + days (unconditional).
    * ``subtract`` — new = current - days. Guarded: new must be ≥ now()
      (422 otherwise); 409 if current is NULL.

    Cascades to all ``proxy_inventory`` of this order (expires_at =
    new) AND to ``traffic_accounts.expires_at`` for pergb orders.
    """
    result = await asyncio.to_thread(_sync_change_expiry, order_ref, payload.mode, payload.days)
    if result["error"] == "not_found":
        raise HTTPException(status_code=404, detail="order not found")
    if result["error"] == "null_base":
        raise HTTPException(
            status_code=409,
            detail="order has no expiry — cannot subtract from NULL",
        )
    if result["error"] == "past":
        raise HTTPException(
            status_code=422,
            detail="resulting expiry lies in the past",
        )
    response = AdminChangeExpiryResponse(
        order_ref=order_ref,
        mode=payload.mode,
        days=payload.days,
        old_expires_at=result["old_expires_at"],
        new_expires_at=result["new_expires_at"],
        affected_inventory_count=result["affected_inventory_count"],
    )
    return JSONResponse(content=response.model_dump(mode="json"))


def _sync_change_expiry(order_ref: str, mode: str, days: int) -> dict[str, Any]:
    """Compute target expiry + atomic cascade across the order row,
    proxy_inventory rows, and traffic_accounts.expires_at."""
    now = datetime.now(tz=UTC)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            select id, proxies_expires_at
              from orders
             where order_ref = %s
             for update
            """,
            (order_ref,),
        )
        order = cur.fetchone()
        if order is None:
            return {"error": "not_found"}
        old: datetime | None = order["proxies_expires_at"]

        if mode == "set":
            new = now.replace(microsecond=0) + _days(days)
        elif mode == "add":
            base = old if old is not None else now
            new = base + _days(days)
        else:  # subtract
            if old is None:
                return {"error": "null_base"}
            new = old - _days(days)
        if new < now:
            return {"error": "past"}

        cur.execute(
            "update orders set proxies_expires_at = %s, updated_at = now() where id = %s",
            (new, order["id"]),
        )
        cur.execute(
            """
            update proxy_inventory
               set expires_at = %s,
                   updated_at = now()
             where order_id = %s
               and status in ('sold', 'expired_grace')
            """,
            (new, order["id"]),
        )
        affected = cur.rowcount
        cur.execute(
            """
            update traffic_accounts
               set expires_at = %s,
                   updated_at = now()
             where order_id = %s
            """,
            (new, order["id"]),
        )
    return {
        "error": "",
        "old_expires_at": old,
        "new_expires_at": new,
        "affected_inventory_count": affected,
    }


def _days(n: int) -> Any:
    """timedelta sugar for the cascade computations above."""
    from datetime import timedelta

    return timedelta(days=n)
