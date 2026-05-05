"""Admin endpoints: stats, orders search, archive export, pergb force-poll."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from orchestrator.api_schemas import (
    AdminTrafficPollResponse,
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
from orchestrator.db import fetch_all, fetch_one
from orchestrator.traffic_poll import TrafficPollService

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
        left join traffic_accounts t on t.order_id = o.id
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
