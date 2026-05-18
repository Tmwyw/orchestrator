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

import psycopg
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb

from orchestrator.api_schemas import (
    BindingCreateRequest,
    BindingItem,
    BindingListResponse,
    BindingUpdateRequest,
    GeoListResponse,
    GeoUsageItem,
    PergbTierItem,
    PergbTiersPutRequest,
    PergbTiersResponse,
    ProblemResponse,
    ProductKindItem,
    ProductKindListResponse,
    SkuAdminDetail,
    SkuAdminItem,
    SkuCreateRequest,
    SkuListResponse,
    SkuStockBreakdownItem,
    SkuUpdateRequest,
)
from orchestrator.db import connect, fetch_all, fetch_one

admin_catalog_router = APIRouter(prefix="/v1/admin")


def _problem(status_code: int, error: str, **extra: Any) -> JSONResponse:
    payload = ProblemResponse(error=error, extra=extra or None).model_dump(exclude_none=True, mode="json")
    return JSONResponse(status_code=status_code, content=payload)


def _audit(
    cur: psycopg.Cursor,
    action: str,
    target_type: str,
    target_id: str | int | None,
    details: dict[str, Any] | None = None,
    actor: str = "admin",
) -> None:
    """Append one row to ``admin_audit_log`` inside the caller's txn.

    Pass the caller's cursor so the audit write is atomic with the
    mutating SQL — a rollback of the parent statement also rolls back
    the audit row.
    """
    cur.execute(
        """
        INSERT INTO admin_audit_log (actor, action, target_type, target_id, details)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            actor,
            action,
            target_type,
            str(target_id) if target_id is not None else None,
            Jsonb(details or {}),
        ),
    )


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


# === POST /v1/admin/skus ===


def _create_sku_sync(payload: SkuCreateRequest) -> dict[str, Any] | str:
    """Insert SKU + audit row inside one transaction.

    Returns the inserted row on success, or a string error code:
      - ``"duplicate_code"`` if ``code`` UNIQUE constraint trips
      - ``"duplicate_kind_geo_protocol"`` if (kind, geo, protocol) already exists

    The (kind, geo, protocol) uniqueness is enforced in application code
    (no DB constraint yet) — we SELECT-check inside the same txn before
    INSERT. Race window is acceptable: a concurrent POST will succeed in
    one branch and the other gets the ``UNIQUE(code)`` violation since
    callers normally derive ``code`` from those three fields anyway.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM skus
            WHERE product_kind = %s AND geo_code = %s AND protocol = %s
            LIMIT 1
            """,
            (payload.product_kind, payload.geo_code, payload.protocol),
        )
        if cur.fetchone():
            return "duplicate_kind_geo_protocol"

        try:
            cur.execute(
                """
                INSERT INTO skus (
                    code, product_kind, geo_code, protocol, duration_days,
                    price_per_piece, price_per_gb, target_stock,
                    refill_batch_size, validation_require_ipv6, is_active
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, code, product_kind, geo_code, protocol,
                          duration_days, price_per_piece, price_per_gb,
                          target_stock, refill_batch_size,
                          validation_require_ipv6, is_active, metadata,
                          created_at, updated_at
                """,
                (
                    payload.code,
                    payload.product_kind,
                    payload.geo_code,
                    payload.protocol,
                    payload.duration_days,
                    payload.price_per_piece,
                    payload.price_per_gb,
                    payload.target_stock,
                    payload.refill_batch_size,
                    payload.validation_require_ipv6,
                    payload.is_active,
                ),
            )
        except psycopg.errors.UniqueViolation:
            return "duplicate_code"

        row = cur.fetchone()
        assert row is not None
        _audit(
            cur,
            action="sku_created",
            target_type="sku",
            target_id=row["id"],
            details=payload.model_dump(mode="json"),
        )
        return dict(row)


_PATCHABLE_FIELDS = (
    "price_per_piece",
    "price_per_gb",
    "target_stock",
    "refill_batch_size",
    "duration_days",
    "validation_require_ipv6",
    "is_active",
)


def _update_sku_sync(sku_id: int, payload: SkuUpdateRequest) -> dict[str, Any] | str:
    """Apply partial update + audit diff. Returns row, or error code.

    Audit ``details`` records ``old``/``new`` snapshots of only the
    changed fields — easy to render as a diff later.
    """
    update_fields = payload.model_dump(exclude_none=True)
    if not update_fields:
        return "no_fields_to_update"

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, code, product_kind, geo_code, protocol, duration_days,
                   price_per_piece, price_per_gb, target_stock, refill_batch_size,
                   validation_require_ipv6, is_active, metadata,
                   created_at, updated_at
            FROM skus
            WHERE id = %s
            FOR UPDATE
            """,
            (sku_id,),
        )
        old_row = cur.fetchone()
        if not old_row:
            return "sku_not_found"

        set_clauses = [f"{col} = %s" for col in update_fields]
        set_clauses.append("updated_at = now()")
        params = list(update_fields.values()) + [sku_id]
        cur.execute(
            f"""
            UPDATE skus
               SET {", ".join(set_clauses)}
             WHERE id = %s
            RETURNING id, code, product_kind, geo_code, protocol, duration_days,
                      price_per_piece, price_per_gb, target_stock, refill_batch_size,
                      validation_require_ipv6, is_active, metadata,
                      created_at, updated_at
            """,
            tuple(params),
        )
        new_row = cur.fetchone()
        assert new_row is not None

        diff = {
            col: {"old": old_row[col], "new": new_row[col]}
            for col in update_fields
            if old_row[col] != new_row[col]
        }
        _audit(
            cur,
            action="sku_updated",
            target_type="sku",
            target_id=sku_id,
            details={"diff": _jsonify_diff(diff)},
        )
        return dict(new_row)


def _jsonify_diff(diff: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Coerce Decimal/datetime values in a diff dict to JSON-safe strings."""
    out: dict[str, dict[str, Any]] = {}
    for col, vals in diff.items():
        out[col] = {
            "old": _jsonify_scalar(vals["old"]),
            "new": _jsonify_scalar(vals["new"]),
        }
    return out


def _jsonify_scalar(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    return str(v)


def _delete_sku_sync(sku_id: int) -> dict[str, Any] | str:
    """Soft-delete SKU. Blocks if any non-terminal orders exist.

    Returns:
      - dict with deleted row on success
      - ``"sku_not_found"`` if id unknown
      - ``"pending_orders"`` if any order with status in
        (``reserved``, ``committed``) still has an active expiry

    Idempotent: re-deleting an already-inactive SKU returns the row but
    still audits the event (so an operator-initiated re-delete is
    visible). Inventory rows are not touched — refill worker will drain
    via natural expiry.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, code, is_active FROM skus WHERE id = %s FOR UPDATE",
            (sku_id,),
        )
        sku_row = cur.fetchone()
        if not sku_row:
            return "sku_not_found"

        cur.execute(
            """
            SELECT count(*)::int AS n
              FROM orders
             WHERE sku_id = %s
               AND status IN ('reserved', 'committed')
               AND (
                     (status = 'reserved'  AND expires_at > now()) OR
                     (status = 'committed' AND (proxies_expires_at IS NULL
                                                OR proxies_expires_at > now()))
                   )
            """,
            (sku_id,),
        )
        pending_row = cur.fetchone()
        pending = int(pending_row["n"]) if pending_row else 0
        if pending > 0:
            return "pending_orders"

        cur.execute(
            """
            UPDATE skus
               SET is_active = false, updated_at = now()
             WHERE id = %s
            RETURNING id, code, is_active, updated_at
            """,
            (sku_id,),
        )
        deleted = cur.fetchone()
        assert deleted is not None
        _audit(
            cur,
            action="sku_deleted",
            target_type="sku",
            target_id=sku_id,
            details={"code": sku_row["code"], "was_active": bool(sku_row["is_active"])},
        )
        return dict(deleted)


@admin_catalog_router.patch("/skus/{sku_id}")
async def patch_sku(sku_id: int, payload: SkuUpdateRequest) -> JSONResponse:
    """Partial update for SKU mutable fields.

    Returns 200 with the updated row + freshly-computed stock breakdown
    so the caller doesn't need a second GET. Returns 400 if the request
    body had zero fields to update, 404 if the SKU doesn't exist.
    """
    result = await asyncio.to_thread(_update_sku_sync, sku_id, payload)
    if result == "no_fields_to_update":
        return _problem(400, "no_fields_to_update")
    if result == "sku_not_found":
        return _problem(404, "sku_not_found")
    assert isinstance(result, dict)
    return await _fresh_sku_detail(result)


@admin_catalog_router.delete("/skus/{sku_id}")
async def delete_sku(sku_id: int) -> JSONResponse:
    """Soft-delete SKU (set ``is_active=false``).

    Blocked with 409 ``pending_orders`` if any order with status
    ``reserved`` or ``committed`` is still within its TTL — operators
    must wait or release explicitly. Restore via direct SQL
    (``UPDATE skus SET is_active=true WHERE id=N``).
    """
    result = await asyncio.to_thread(_delete_sku_sync, sku_id)
    if result == "sku_not_found":
        return _problem(404, "sku_not_found")
    if result == "pending_orders":
        return _problem(409, "pending_orders")
    assert isinstance(result, dict)
    return JSONResponse(content={"success": True, "deleted_id": result["id"]})


async def _fresh_sku_detail(sku_row: dict[str, Any]) -> JSONResponse:
    """Build a SkuAdminDetail response for an existing SKU row.

    Reuses the same per-node breakdown query as ``get_sku`` so PATCH
    responses match GET responses exactly.
    """
    sku_id = sku_row["id"]
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


@admin_catalog_router.post("/skus", status_code=201)
async def create_sku(payload: SkuCreateRequest) -> JSONResponse:
    """Create a new SKU.

    Validations:
      - ``code`` regex ``^[a-z0-9_]+$``, 3-64 chars (in Pydantic model)
      - ``price_per_piece`` / ``price_per_gb`` > 0 and ≤ 10000
      - ``target_stock`` 1..1_000_000
      - UNIQUE (``kind``, ``geo_code``, ``protocol``) — checked in txn
      - UNIQUE ``code`` — enforced by DB index

    On success, audits ``sku_created`` with the full request body in
    ``details``.
    """
    result = await asyncio.to_thread(_create_sku_sync, payload)
    if isinstance(result, str):
        return _problem(409, result)
    detail = SkuAdminDetail(
        **result,
        stock_total={
            "available": 0,
            "reserved": 0,
            "sold": 0,
            "expired_grace": 0,
            "pending_validation": 0,
        },
        stock_breakdown=[],
    )
    return JSONResponse(status_code=201, content=detail.model_dump(mode="json"))


# === /v1/admin/skus/{sku_id}/bindings — CATALOG-1 Phase A.4 ===


def _list_bindings_sync(sku_id: int) -> list[dict[str, Any]] | str:
    sku = fetch_one("SELECT 1 FROM skus WHERE id = %s", (sku_id,))
    if not sku:
        return "sku_not_found"
    return fetch_all(
        """
        SELECT b.node_id, n.name AS node_name, n.geo AS node_geo,
               b.weight, b.max_batch_size, b.is_active,
               b.created_at, b.updated_at
          FROM sku_node_bindings b
          JOIN nodes n ON n.id = b.node_id
         WHERE b.sku_id = %s
         ORDER BY n.name
        """,
        (sku_id,),
    )


def _add_binding_sync(sku_id: int, payload: BindingCreateRequest) -> dict[str, Any] | str:
    """Bind a node to an SKU with geo validation.

    Geo rule: ``node.geo`` must equal ``sku.geo_code``, OR ``sku.geo_code``
    must be empty (datacenter_pergb / global SKUs). This mirrors the
    auto-bind path in ``/v1/nodes/enroll`` and prevents accidental cross-
    geo allocations that would surprise the buyer.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, geo_code FROM skus WHERE id = %s", (sku_id,))
        sku = cur.fetchone()
        if not sku:
            return "sku_not_found"
        cur.execute("SELECT id, name, geo FROM nodes WHERE id = %s", (payload.node_id,))
        node = cur.fetchone()
        if not node:
            return "node_not_found"

        sku_geo = (sku["geo_code"] or "").strip()
        node_geo = (node["geo"] or "").strip()
        if sku_geo and node_geo != sku_geo:
            return "geo_mismatch"

        try:
            cur.execute(
                """
                INSERT INTO sku_node_bindings
                    (sku_id, node_id, weight, max_batch_size, is_active)
                VALUES (%s, %s, %s, %s, true)
                RETURNING node_id, weight, max_batch_size, is_active,
                          created_at, updated_at
                """,
                (sku_id, payload.node_id, payload.weight, payload.max_batch_size),
            )
        except psycopg.errors.UniqueViolation:
            return "binding_exists"
        row = cur.fetchone()
        assert row is not None
        result = {
            **dict(row),
            "node_name": node["name"],
            "node_geo": node["geo"] or "",
        }
        _audit(
            cur,
            action="binding_added",
            target_type="binding",
            target_id=f"{sku_id}:{payload.node_id}",
            details={
                "sku_id": sku_id,
                "node_id": payload.node_id,
                "weight": payload.weight,
                "max_batch_size": payload.max_batch_size,
            },
        )
        return result


def _update_binding_sync(sku_id: int, node_id: str, payload: BindingUpdateRequest) -> dict[str, Any] | str:
    update_fields = payload.model_dump(exclude_none=True)
    if not update_fields:
        return "no_fields_to_update"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.node_id, n.name AS node_name, n.geo AS node_geo,
                   b.weight, b.max_batch_size, b.is_active
              FROM sku_node_bindings b
              JOIN nodes n ON n.id = b.node_id
             WHERE b.sku_id = %s AND b.node_id = %s
             FOR UPDATE
            """,
            (sku_id, node_id),
        )
        old = cur.fetchone()
        if not old:
            return "binding_not_found"

        set_clauses = [f"{col} = %s" for col in update_fields]
        set_clauses.append("updated_at = now()")
        params = list(update_fields.values()) + [sku_id, node_id]
        cur.execute(
            f"""
            UPDATE sku_node_bindings
               SET {", ".join(set_clauses)}
             WHERE sku_id = %s AND node_id = %s
            RETURNING node_id, weight, max_batch_size, is_active,
                      created_at, updated_at
            """,
            tuple(params),
        )
        new_row = cur.fetchone()
        assert new_row is not None
        diff = {
            col: {"old": old[col], "new": new_row[col]} for col in update_fields if old[col] != new_row[col]
        }
        _audit(
            cur,
            action="binding_updated",
            target_type="binding",
            target_id=f"{sku_id}:{node_id}",
            details={"diff": _jsonify_diff(diff)},
        )
        return {
            **dict(new_row),
            "node_name": old["node_name"],
            "node_geo": old["node_geo"] or "",
        }


def _delete_binding_sync(sku_id: int, node_id: str) -> dict[str, Any] | str:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sku_node_bindings WHERE sku_id = %s AND node_id = %s",
            (sku_id, node_id),
        )
        if not cur.fetchone():
            return "binding_not_found"
        cur.execute(
            "DELETE FROM sku_node_bindings WHERE sku_id = %s AND node_id = %s",
            (sku_id, node_id),
        )
        _audit(
            cur,
            action="binding_removed",
            target_type="binding",
            target_id=f"{sku_id}:{node_id}",
            details={"sku_id": sku_id, "node_id": node_id},
        )
        return {"sku_id": sku_id, "node_id": node_id}


@admin_catalog_router.get("/skus/{sku_id}/bindings")
async def list_bindings(sku_id: int) -> JSONResponse:
    """List all node bindings for an SKU."""
    result = await asyncio.to_thread(_list_bindings_sync, sku_id)
    if result == "sku_not_found":
        return _problem(404, "sku_not_found")
    assert isinstance(result, list)
    items = [BindingItem(**r) for r in result]
    return JSONResponse(content=BindingListResponse(items=items).model_dump(mode="json"))


@admin_catalog_router.post("/skus/{sku_id}/bindings", status_code=201)
async def add_binding(sku_id: int, payload: BindingCreateRequest) -> JSONResponse:
    """Add a node binding for an SKU with geo validation."""
    result = await asyncio.to_thread(_add_binding_sync, sku_id, payload)
    error_status: dict[str, int] = {
        "sku_not_found": 404,
        "node_not_found": 404,
        "geo_mismatch": 409,
        "binding_exists": 409,
    }
    if isinstance(result, str):
        return _problem(error_status[result], result)
    return JSONResponse(status_code=201, content=BindingItem(**result).model_dump(mode="json"))


@admin_catalog_router.patch("/skus/{sku_id}/bindings/{node_id}")
async def patch_binding(sku_id: int, node_id: str, payload: BindingUpdateRequest) -> JSONResponse:
    """Update weight, max_batch_size, or is_active for a binding."""
    result = await asyncio.to_thread(_update_binding_sync, sku_id, node_id, payload)
    if result == "no_fields_to_update":
        return _problem(400, "no_fields_to_update")
    if result == "binding_not_found":
        return _problem(404, "binding_not_found")
    assert isinstance(result, dict)
    return JSONResponse(content=BindingItem(**result).model_dump(mode="json"))


@admin_catalog_router.delete("/skus/{sku_id}/bindings/{node_id}")
async def delete_binding(sku_id: int, node_id: str) -> JSONResponse:
    """Remove a node binding (hard delete — no inventory cascade)."""
    result = await asyncio.to_thread(_delete_binding_sync, sku_id, node_id)
    if result == "binding_not_found":
        return _problem(404, "binding_not_found")
    assert isinstance(result, dict)
    return JSONResponse(content={"success": True, "sku_id": result["sku_id"], "node_id": result["node_id"]})


# === /v1/admin/skus/{sku_id}/tiers — CATALOG-1 Phase A.5 ===
#
# Tiers are stored in the dedicated ``sku_tiers`` table (migration 024),
# NOT in ``skus.metadata.tiers`` as the original plan text suggested.
# Reasoning recorded in wave_catalog1_plan.md "Решения / отклонения".


def _list_tiers_sync(sku_id: int) -> list[dict[str, Any]] | str:
    sku = fetch_one("SELECT 1 FROM skus WHERE id = %s", (sku_id,))
    if not sku:
        return "sku_not_found"
    return fetch_all(
        """
        SELECT gb, price_per_gb
          FROM sku_tiers
         WHERE sku_id = %s AND is_active = TRUE
         ORDER BY gb ASC
        """,
        (sku_id,),
    )


def _replace_tiers_sync(sku_id: int, payload: PergbTiersPutRequest) -> list[dict[str, Any]] | str:
    """Atomically replace the tier table for an SKU.

    Uses a single txn: soft-delete all existing tiers (is_active=false)
    then insert the new ones. The bot's /v1/skus/active reader filters
    by is_active so a partial intermediate state is never visible. We
    soft-delete rather than DELETE so historical tier values stay
    queryable for audit / debugging.

    Validates that the target SKU is product_kind=datacenter_pergb —
    tiers are only meaningful there.
    """
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, product_kind FROM skus WHERE id = %s FOR UPDATE",
            (sku_id,),
        )
        sku = cur.fetchone()
        if not sku:
            return "sku_not_found"
        if sku["product_kind"] != "datacenter_pergb":
            return "sku_not_pergb"

        cur.execute(
            "UPDATE sku_tiers SET is_active = FALSE, updated_at = now() "
            "WHERE sku_id = %s AND is_active = TRUE",
            (sku_id,),
        )

        new_rows: list[dict[str, Any]] = []
        for tier in payload.tiers:
            # Re-insert: there might be an existing (sku_id, gb) row from
            # a previous version since UNIQUE(sku_id, gb) is on the table.
            # Upsert pattern resurrects soft-deleted rows in-place rather
            # than inserting a sibling.
            cur.execute(
                """
                INSERT INTO sku_tiers (sku_id, gb, price_per_gb, is_active)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (sku_id, gb) DO UPDATE
                  SET price_per_gb = EXCLUDED.price_per_gb,
                      is_active    = TRUE,
                      updated_at   = now()
                RETURNING gb, price_per_gb
                """,
                (sku_id, tier.gb, tier.price_per_gb),
            )
            row = cur.fetchone()
            assert row is not None
            new_rows.append(dict(row))

        _audit(
            cur,
            action="tiers_replaced",
            target_type="sku_tiers",
            target_id=sku_id,
            details={"tiers": [{"gb": t.gb, "price_per_gb": str(t.price_per_gb)} for t in payload.tiers]},
        )
        return new_rows


@admin_catalog_router.get("/skus/{sku_id}/tiers")
async def list_tiers(sku_id: int) -> JSONResponse:
    """List active pergb tiers for an SKU.

    Returns 404 if the SKU does not exist. An SKU with no active tiers
    returns ``{items: []}``.
    """
    result = await asyncio.to_thread(_list_tiers_sync, sku_id)
    if result == "sku_not_found":
        return _problem(404, "sku_not_found")
    assert isinstance(result, list)
    items = [PergbTierItem(**r) for r in result]
    return JSONResponse(content=PergbTiersResponse(items=items).model_dump(mode="json"))


# Hardcoded product_kind catalog — mirrors the CHECK constraint on
# skus.product_kind (migration 005) and PRODUCT_KIND_NAMES in main.py.
# Add a row here when adding a new value to the CHECK constraint.
_PRODUCT_KIND_LABELS: dict[str, str] = {
    "ipv6": "IPv6 SOCKS5",
    "datacenter_pergb": "Pay-per-GB Datacenter",
}


@admin_catalog_router.put("/skus/{sku_id}/tiers")
async def put_tiers(sku_id: int, payload: PergbTiersPutRequest) -> JSONResponse:
    """Atomic replace of the pergb tier table for an SKU.

    Pydantic enforces:
      - ``gb`` strictly ascending across the list
      - ``price_per_gb`` monotonically non-increasing (cheaper-or-equal
        at higher quantities)
      - 1..100 tiers per SKU

    Endpoint checks the SKU exists and is ``product_kind=datacenter_pergb``
    (returns 400 ``sku_not_pergb`` otherwise — tiers on a per-piece SKU
    are meaningless).
    """
    result = await asyncio.to_thread(_replace_tiers_sync, sku_id, payload)
    if result == "sku_not_found":
        return _problem(404, "sku_not_found")
    if result == "sku_not_pergb":
        return _problem(400, "sku_not_pergb")
    assert isinstance(result, list)
    items = [PergbTierItem(**r) for r in result]
    return JSONResponse(content=PergbTiersResponse(items=items).model_dump(mode="json"))


# === /v1/admin/geos, /v1/admin/product_kinds — CATALOG-1 Phase A.6 ===


@admin_catalog_router.get("/geos")
async def list_geos() -> JSONResponse:
    """List used geo codes with SKU counts.

    Reads ``DISTINCT geo_code FROM skus WHERE geo_code != ''`` —
    pergb / global SKUs (geo_code='') are intentionally excluded since
    "no geo" isn't a real geo to manage. Returns empty list if no SKUs
    have a populated geo_code.
    """
    rows = await asyncio.to_thread(
        fetch_all,
        """
        SELECT geo_code, COUNT(*)::int AS sku_count
          FROM skus
         WHERE geo_code <> ''
         GROUP BY geo_code
         ORDER BY geo_code
        """,
    )
    items = [GeoUsageItem(**r) for r in rows]
    return JSONResponse(content=GeoListResponse(items=items).model_dump(mode="json"))


@admin_catalog_router.get("/product_kinds")
async def list_product_kinds() -> JSONResponse:
    """List known product_kind values + usage counts.

    The list of kinds is hardcoded in ``_PRODUCT_KIND_LABELS`` (mirrors
    the CHECK constraint on ``skus.product_kind``). For each kind we
    return its human-readable name and how many SKUs use it currently —
    useful for the bot's "🏷 Типы прокси" read-only page.
    """
    rows = await asyncio.to_thread(
        fetch_all,
        """
        SELECT product_kind, COUNT(*)::int AS sku_count
          FROM skus
         GROUP BY product_kind
        """,
    )
    counts = {r["product_kind"]: int(r["sku_count"]) for r in rows}
    items = [
        ProductKindItem(kind=kind, name=label, sku_count=counts.get(kind, 0))
        for kind, label in _PRODUCT_KIND_LABELS.items()
    ]
    return JSONResponse(content=ProductKindListResponse(items=items).model_dump(mode="json"))
