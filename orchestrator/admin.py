"""Admin endpoints: stats, orders search, archive export."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from orchestrator.api_schemas import (
    ArchiveExportItem,
    ArchiveExportResponse,
    OrderListItem,
    OrdersListResponse,
    StatsInventoryRow,
    StatsNodes,
    StatsResponse,
    StatsSales,
)
from orchestrator.db import fetch_all, fetch_one

admin_router = APIRouter(prefix="/v1/admin")


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
    response = StatsResponse(
        sales=StatsSales(**(sales_row or {})),
        inventory=[StatsInventoryRow(**r) for r in inventory_rows],
        nodes=StatsNodes(**(nodes_row or {})),
    )
    return JSONResponse(content=response.model_dump(mode="json"))


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
