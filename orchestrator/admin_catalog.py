"""Admin catalog endpoints — SKU/bindings/tiers/geo CRUD.

Wave CATALOG-1 Phase A. Foundation for the bot's "🏪 Ассортимент"
admin GUI. All routes mount under ``/v1/admin/...`` and are protected by
``require_api_key`` at the include_router level (see ``main.py``).

Separate router from ``orchestrator/admin.py`` so the read-only stats /
orders endpoints stay independent of the write-side catalog router and
so each can be feature-flagged off if needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from orchestrator.api_schemas import (
    ProblemResponse,
    SkuAdminDetail,
    SkuAdminItem,
    SkuListResponse,
    SkuStockBreakdownItem,
)
from orchestrator.db import fetch_all, fetch_one

admin_catalog_router = APIRouter(prefix="/v1/admin")


def _problem(status_code: int, error: str, **extra: Any) -> JSONResponse:
    payload = ProblemResponse(error=error, extra=extra or None).model_dump(
        exclude_none=True, mode="json"
    )
    return JSONResponse(status_code=status_code, content=payload)


# === GET /v1/admin/skus ===


@admin_catalog_router.get("/skus")
async def list_skus(
    kind: str | None = None,
    geo: str | None = None,
    is_active: bool | None = None,
) -> JSONResponse:
    """List all SKUs (including inactive) with optional filters.

    Query params:
      - ``kind``: filter by ``product_kind`` (e.g. ``ipv6``, ``datacenter_pergb``)
      - ``geo``:  filter by ``geo_code`` (case-sensitive — caller normalizes)
      - ``is_active``: ``true``/``false`` to filter

    Response: ``{items: [SkuAdminItem], total: N}``. ``stock_available``
    is the count of ``proxy_inventory`` rows in ``status='available'``.
    """
    where: list[str] = ["1=1"]
    params: list[Any] = []
    if kind is not None:
        where.append("s.product_kind = %s")
        params.append(kind)
    if geo is not None:
        where.append("s.geo_code = %s")
        params.append(geo)
    if is_active is not None:
        where.append("s.is_active = %s")
        params.append(is_active)
    sql = f"""
        SELECT
          s.id, s.code, s.product_kind, s.geo_code, s.protocol,
          s.duration_days, s.price_per_piece, s.price_per_gb,
          s.target_stock, s.refill_batch_size, s.is_active,
          s.created_at, s.updated_at,
          COALESCE(SUM(CASE WHEN pi.status = 'available' THEN 1 ELSE 0 END), 0)::int
            AS stock_available
        FROM skus s
        LEFT JOIN proxy_inventory pi ON pi.sku_id = s.id
        WHERE {" AND ".join(where)}
        GROUP BY s.id
        ORDER BY s.id
    """
    rows = await asyncio.to_thread(fetch_all, sql, tuple(params))
    items = [SkuAdminItem(**r) for r in rows]
    response = SkuListResponse(items=items, total=len(items))
    return JSONResponse(content=response.model_dump(mode="json"))


# === GET /v1/admin/skus/{id} ===


@admin_catalog_router.get("/skus/{sku_id}")
async def get_sku(sku_id: int) -> JSONResponse:
    """Get full SKU details + per-node stock breakdown.

    Breakdown is computed across nodes bound to this SKU via
    ``sku_node_bindings`` — a node with zero inventory for this SKU still
    appears (with all-zero counts) if the binding exists.

    Returns 404 if the SKU does not exist.
    """
    sku_row = await asyncio.to_thread(
        fetch_one,
        """
        SELECT id, code, product_kind, geo_code, protocol, duration_days,
               price_per_piece, price_per_gb, target_stock, refill_batch_size,
               validation_require_ipv6, is_active, metadata,
               created_at, updated_at
        FROM skus
        WHERE id = %s
        """,
        (sku_id,),
    )
    if not sku_row:
        return _problem(404, "sku_not_found")

    breakdown_rows = await asyncio.to_thread(
        fetch_all,
        """
        SELECT
          n.id AS node_id,
          n.name AS node_name,
          COALESCE(SUM(CASE WHEN pi.status = 'available' THEN 1 ELSE 0 END), 0)::int
            AS available,
          COALESCE(SUM(CASE WHEN pi.status = 'reserved' THEN 1 ELSE 0 END), 0)::int
            AS reserved,
          COALESCE(SUM(CASE WHEN pi.status = 'sold' THEN 1 ELSE 0 END), 0)::int
            AS sold,
          COALESCE(SUM(CASE WHEN pi.status = 'expired_grace' THEN 1 ELSE 0 END), 0)::int
            AS expired_grace,
          COALESCE(SUM(CASE WHEN pi.status = 'pending_validation' THEN 1 ELSE 0 END), 0)::int
            AS pending_validation
        FROM sku_node_bindings b
        JOIN nodes n ON n.id = b.node_id
        LEFT JOIN proxy_inventory pi
               ON pi.node_id = n.id AND pi.sku_id = b.sku_id
        WHERE b.sku_id = %s
        GROUP BY n.id, n.name
        ORDER BY n.name
        """,
        (sku_id,),
    )

    stock_total = {
        "available": sum(int(r["available"]) for r in breakdown_rows),
        "reserved": sum(int(r["reserved"]) for r in breakdown_rows),
        "sold": sum(int(r["sold"]) for r in breakdown_rows),
        "expired_grace": sum(int(r["expired_grace"]) for r in breakdown_rows),
        "pending_validation": sum(int(r["pending_validation"]) for r in breakdown_rows),
    }
    detail = SkuAdminDetail(
        **sku_row,
        stock_total=stock_total,
        stock_breakdown=[SkuStockBreakdownItem(**r) for r in breakdown_rows],
    )
    return JSONResponse(content=detail.model_dump(mode="json"))
