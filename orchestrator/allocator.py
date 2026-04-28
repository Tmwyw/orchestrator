"""Allocator service: equal-share reserve/commit/release for orders."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg.types.json import Jsonb

from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.delivery import generate_delivery_content
from orchestrator.distribution import equal_share
from orchestrator.redis_client import get_redis
from orchestrator.schemas import DeliveryFormat, OrderStatus

logger = logging.getLogger("netrun-orchestrator-allocator")

_IDEM_CACHE_TTL_SEC = 24 * 60 * 60


@dataclass(slots=True)
class ReserveResult:
    success: bool
    order_ref: str | None
    expires_at: datetime | None
    proxies_count: int
    error: str | None = None
    available_now: int | None = None


@dataclass(slots=True)
class CommitResult:
    success: bool
    order_ref: str
    status: OrderStatus
    proxies_expires_at: datetime | None
    error: str | None = None


@dataclass(slots=True)
class ReleaseResult:
    success: bool
    order_ref: str
    status: OrderStatus
    released_count: int
    error: str | None = None


@dataclass(slots=True)
class ProxiesResult:
    success: bool
    content: str | None = None
    content_type: str | None = None
    line_count: int = 0
    error: str | None = None
    locked_format: str | None = None


@dataclass(slots=True)
class ExtendResult:
    success: bool
    order_ref: str
    extended_count: int
    new_proxies_expires_at: datetime | None
    error: str | None = None


class AllocatorService:
    """Equal-share allocator with Redis-backed reservation TTL and idempotency."""

    async def reserve(
        self,
        *,
        user_id: int,
        sku_id: int,
        quantity: int,
        reservation_ttl_sec: int,
        idempotency_key: str | None = None,
    ) -> ReserveResult:
        if idempotency_key:
            cached = await self._idem_get(idempotency_key)
            if cached is not None:
                logger.info("reserve idempotent hit key=%s ref=%s", idempotency_key, cached.order_ref)
                return cached

        cfg = get_config()
        sku = await asyncio.to_thread(self._sync_get_active_sku, sku_id)
        if sku is None:
            return ReserveResult(
                success=False,
                order_ref=None,
                expires_at=None,
                proxies_count=0,
                error="sku_not_active",
            )

        bindings = await asyncio.to_thread(
            self._sync_list_active_bindings,
            sku_id,
            cfg.proxy_allow_degraded_nodes,
        )
        if not bindings:
            return ReserveResult(
                success=False,
                order_ref=None,
                expires_at=None,
                proxies_count=0,
                error="no_active_bindings",
            )

        n = len(bindings)
        quotas = equal_share(quantity, [10**9] * n)
        reservation_key = f"resv_{uuid.uuid4().hex}"
        order_ref = "ord_" + uuid.uuid4().hex[:12]
        ttl = max(
            cfg.reservation_min_ttl_sec,
            min(reservation_ttl_sec, cfg.reservation_max_ttl_sec),
        )

        claimed_ids, total = await asyncio.to_thread(
            self._sync_claim_per_node_with_rollback,
            sku_id,
            bindings,
            quotas,
            reservation_key,
        )

        if total < quantity:
            if claimed_ids:
                await asyncio.to_thread(self._sync_release_inventory, claimed_ids, reservation_key)
            available_now = await asyncio.to_thread(self._sync_count_available, sku_id)
            return ReserveResult(
                success=False,
                order_ref=None,
                expires_at=None,
                proxies_count=0,
                error="insufficient_stock",
                available_now=available_now,
            )

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        await asyncio.to_thread(
            self._sync_insert_order,
            order_ref=order_ref,
            user_id=user_id,
            sku_id=sku_id,
            requested_count=quantity,
            allocated_count=total,
            reservation_key=reservation_key,
            expires_at=expires_at,
            idempotency_key=idempotency_key,
        )

        redis = await get_redis()
        await redis.set(
            f"reservation:{order_ref}",
            json.dumps(
                {
                    "order_ref": order_ref,
                    "user_id": user_id,
                    "sku_id": sku_id,
                    "inventory_ids": claimed_ids,
                    "expires_at": expires_at.isoformat(),
                }
            ),
            ex=ttl,
        )

        result = ReserveResult(
            success=True,
            order_ref=order_ref,
            expires_at=expires_at,
            proxies_count=total,
        )
        if idempotency_key:
            await self._idem_set(idempotency_key, result)

        logger.info(
            "reserve success order_ref=%s sku=%s qty=%s claimed=%s ttl=%s",
            order_ref,
            sku_id,
            quantity,
            total,
            ttl,
        )
        return result

    async def commit(self, *, order_ref: str, duration_days: int | None) -> CommitResult:
        order = await asyncio.to_thread(self._sync_get_order, order_ref)
        if order is None:
            return CommitResult(
                success=False,
                order_ref=order_ref,
                status=OrderStatus.RESERVED,
                proxies_expires_at=None,
                error="order_not_found",
            )

        status = str(order["status"])
        if status != OrderStatus.RESERVED.value:
            return CommitResult(
                success=False,
                order_ref=order_ref,
                status=OrderStatus(status),
                proxies_expires_at=None,
                error=f"order_state_{status}",
            )

        expires_at = order["expires_at"]
        if isinstance(expires_at, datetime) and expires_at <= datetime.now(timezone.utc):
            return CommitResult(
                success=False,
                order_ref=order_ref,
                status=OrderStatus.RESERVED,
                proxies_expires_at=None,
                error="reservation_expired",
            )

        sku = await asyncio.to_thread(self._sync_get_sku_any, int(order["sku_id"]))
        days = duration_days if duration_days is not None else int((sku or {}).get("duration_days") or 30)

        updated = await asyncio.to_thread(self._sync_commit_order, order_ref, days)
        if updated is None:
            return CommitResult(
                success=False,
                order_ref=order_ref,
                status=OrderStatus.RESERVED,
                proxies_expires_at=None,
                error="commit_failed",
            )

        redis = await get_redis()
        await redis.delete(f"reservation:{order_ref}")

        logger.info(
            "commit success order_ref=%s sku=%s expires_at=%s",
            order_ref,
            order["sku_id"],
            updated.get("proxies_expires_at"),
        )
        return CommitResult(
            success=True,
            order_ref=order_ref,
            status=OrderStatus.COMMITTED,
            proxies_expires_at=updated.get("proxies_expires_at"),
        )

    async def release(self, *, order_ref: str) -> ReleaseResult:
        order = await asyncio.to_thread(self._sync_get_order, order_ref)
        if order is None:
            return ReleaseResult(
                success=False,
                order_ref=order_ref,
                status=OrderStatus.RESERVED,
                released_count=0,
                error="order_not_found",
            )

        status = str(order["status"])
        if status != OrderStatus.RESERVED.value:
            return ReleaseResult(
                success=False,
                order_ref=order_ref,
                status=OrderStatus(status),
                released_count=0,
                error=f"order_state_{status}",
            )

        released_count, _updated = await asyncio.to_thread(self._sync_release_order, order_ref)

        redis = await get_redis()
        await redis.delete(f"reservation:{order_ref}")

        logger.info("release success order_ref=%s released=%s", order_ref, released_count)
        return ReleaseResult(
            success=True,
            order_ref=order_ref,
            status=OrderStatus.RELEASED,
            released_count=released_count,
        )

    async def get_proxies(self, *, order_ref: str, format: DeliveryFormat) -> ProxiesResult:
        """Lazy-create or fetch the delivery file content for a committed order."""
        order = await asyncio.to_thread(self._sync_get_order, order_ref)
        if order is None:
            return ProxiesResult(success=False, error="order_not_found")
        if str(order["status"]) != OrderStatus.COMMITTED.value:
            return ProxiesResult(success=False, error="order_not_committed")

        existing = await asyncio.to_thread(self._sync_get_delivery_file, int(order["id"]))

        if existing and str(existing["format"]) != format.value:
            return ProxiesResult(
                success=False,
                error="format_locked",
                locked_format=str(existing["format"]),
            )

        if existing and existing.get("content") is not None:
            content = str(existing["content"])
            content_type = "application/json" if format == DeliveryFormat.JSON else "text/plain"
            return ProxiesResult(
                success=True,
                content=content,
                content_type=content_type,
                line_count=int(existing["line_count"]),
            )

        rows = await asyncio.to_thread(self._sync_list_inventory_for_order, int(order["id"]))
        if not rows:
            return ProxiesResult(success=False, error="inventory_empty")

        content, content_type = generate_delivery_content(rows, format)
        line_count = len(rows)
        checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
        content_expires_at = datetime.now(timezone.utc) + timedelta(days=30)

        await asyncio.to_thread(
            self._sync_upsert_delivery_file,
            order_id=int(order["id"]),
            format=format.value,
            content=content,
            line_count=line_count,
            checksum=checksum,
            content_expires_at=content_expires_at,
        )

        logger.info(
            "delivery file generated order_ref=%s format=%s line_count=%s",
            order_ref,
            format.value,
            line_count,
        )
        return ProxiesResult(
            success=True,
            content=content,
            content_type=content_type,
            line_count=line_count,
        )

    async def extend_order(
        self,
        *,
        order_ref: str,
        duration_days: int,
        inventory_ids: list[int] | None = None,
        geo_code: str | None = None,
    ) -> ExtendResult:
        """Extend ``expires_at`` for an order's inventory (whole / by_ids / by_geo)."""
        order = await asyncio.to_thread(self._sync_get_order, order_ref)
        if order is None:
            return ExtendResult(
                success=False,
                order_ref=order_ref,
                extended_count=0,
                new_proxies_expires_at=None,
                error="order_not_found",
            )
        if str(order["status"]) != OrderStatus.COMMITTED.value:
            return ExtendResult(
                success=False,
                order_ref=order_ref,
                extended_count=0,
                new_proxies_expires_at=None,
                error=f"order_state_{order['status']}",
            )

        extended, new_expires = await asyncio.to_thread(
            self._sync_extend_inventory,
            order_id=int(order["id"]),
            duration_days=duration_days,
            inventory_ids=inventory_ids,
            geo_code=geo_code,
        )

        if extended == 0:
            return ExtendResult(
                success=False,
                order_ref=order_ref,
                extended_count=0,
                new_proxies_expires_at=None,
                error="no_inventory_matched",
            )

        logger.info(
            "extend success order_ref=%s duration_days=%s extended=%s",
            order_ref,
            duration_days,
            extended,
        )
        return ExtendResult(
            success=True,
            order_ref=order_ref,
            extended_count=extended,
            new_proxies_expires_at=new_expires,
        )

    # === Sync DB helpers (run inside asyncio.to_thread) ===

    def _sync_get_active_sku(self, sku_id: int) -> dict[str, Any] | None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select * from skus where id = %s and is_active = true",
                (sku_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def _sync_get_sku_any(self, sku_id: int) -> dict[str, Any] | None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("select * from skus where id = %s", (sku_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def _sync_list_active_bindings(self, sku_id: int, allow_degraded: bool) -> list[dict[str, Any]]:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select
                  b.sku_id,
                  b.node_id,
                  b.weight as binding_weight,
                  least(b.max_batch_size, n.max_batch_size) as effective_max_batch,
                  n.max_parallel_jobs as max_parallel_jobs,
                  n.runtime_status as runtime_status
                from sku_node_bindings b
                join nodes n on n.id = b.node_id
                where b.sku_id = %s
                  and b.is_active = true
                  and (
                        n.runtime_status = 'active'
                        or (%s and n.runtime_status = 'degraded')
                      )
                order by b.id
                """,
                (sku_id, allow_degraded),
            )
            return [dict(r) for r in cur.fetchall()]

    def _sync_claim_per_node_with_rollback(
        self,
        sku_id: int,
        bindings: list[dict[str, Any]],
        quotas: list[int],
        reservation_key: str,
    ) -> tuple[list[int], int]:
        """Claim quotas across nodes in ONE transaction. Returns (ids, total)."""
        all_ids: list[int] = []
        with connect() as conn:
            for binding, quota in zip(bindings, quotas, strict=True):
                if quota <= 0:
                    continue
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        with selected as (
                            select id from proxy_inventory
                            where sku_id = %s and node_id = %s and status = 'available'
                            order by id
                            for update skip locked
                            limit %s
                        )
                        update proxy_inventory
                        set status = 'reserved',
                            reservation_key = %s,
                            reserved_at = now(),
                            updated_at = now()
                        where id in (select id from selected)
                        returning id
                        """,
                        (sku_id, binding["node_id"], quota, reservation_key),
                    )
                    ids = [int(r["id"]) for r in cur.fetchall()]
                all_ids.extend(ids)
        return all_ids, len(all_ids)

    def _sync_release_inventory(self, inventory_ids: list[int], reservation_key: str) -> int:
        if not inventory_ids:
            return 0
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update proxy_inventory
                set status = 'available',
                    reservation_key = null,
                    reserved_at = null,
                    updated_at = now()
                where id = any(%s)
                  and reservation_key = %s
                  and status = 'reserved'
                returning id
                """,
                (inventory_ids, reservation_key),
            )
            return len(cur.fetchall())

    def _sync_count_available(self, sku_id: int) -> int:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select count(*) as c from proxy_inventory where sku_id = %s and status = 'available'",
                (sku_id,),
            )
            row = cur.fetchone() or {}
        return int(row.get("c") or 0)

    def _sync_insert_order(
        self,
        *,
        order_ref: str,
        user_id: int,
        sku_id: int,
        requested_count: int,
        allocated_count: int,
        reservation_key: str,
        expires_at: datetime,
        idempotency_key: str | None,
    ) -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into orders (
                  order_ref, user_id, sku_id, status,
                  requested_count, allocated_count,
                  reservation_key, reserved_at, expires_at,
                  idempotency_key, metadata
                )
                values (
                  %s, %s, %s, 'reserved',
                  %s, %s,
                  %s, now(), %s,
                  %s, %s
                )
                """,
                (
                    order_ref,
                    user_id,
                    sku_id,
                    requested_count,
                    allocated_count,
                    reservation_key,
                    expires_at,
                    idempotency_key,
                    Jsonb({}),
                ),
            )

    def _sync_get_order(self, order_ref: str) -> dict[str, Any] | None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("select * from orders where order_ref = %s", (order_ref,))
            row = cur.fetchone()
        return dict(row) if row else None

    def _sync_commit_order(self, order_ref: str, duration_days: int) -> dict[str, Any] | None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update orders
                set status = 'committed',
                    committed_at = now(),
                    proxies_expires_at = now() + (%s || ' days')::interval,
                    updated_at = now()
                where order_ref = %s and status = 'reserved'
                returning *
                """,
                (duration_days, order_ref),
            )
            order_row = cur.fetchone()
            if not order_row:
                return None
            order = dict(order_row)
            cur.execute(
                """
                update proxy_inventory
                set status = 'sold',
                    sold_at = now(),
                    expires_at = %s,
                    order_id = %s,
                    updated_at = now()
                where reservation_key = %s and status = 'reserved'
                """,
                (order["proxies_expires_at"], order["id"], order["reservation_key"]),
            )
        return order

    def _sync_release_order(self, order_ref: str) -> tuple[int, dict[str, Any] | None]:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update orders
                set status = 'released',
                    released_at = now(),
                    updated_at = now()
                where order_ref = %s and status = 'reserved'
                returning *
                """,
                (order_ref,),
            )
            order_row = cur.fetchone()
            if not order_row:
                return 0, None
            order = dict(order_row)
            cur.execute(
                """
                update proxy_inventory
                set status = 'available',
                    reservation_key = null,
                    reserved_at = null,
                    updated_at = now()
                where reservation_key = %s and status = 'reserved'
                returning id
                """,
                (order["reservation_key"],),
            )
            released = len(cur.fetchall())
        return released, order

    def _sync_get_delivery_file(self, order_id: int) -> dict[str, Any] | None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("select * from delivery_files where order_id = %s", (order_id,))
            row = cur.fetchone()
        return dict(row) if row else None

    def _sync_list_inventory_for_order(self, order_id: int) -> list[dict[str, Any]]:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select id, host, port, login, password, expires_at, geo_country
                from proxy_inventory
                where order_id = %s and status in ('sold', 'expired_grace')
                order by id
                """,
                (order_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def _sync_upsert_delivery_file(
        self,
        *,
        order_id: int,
        format: str,
        content: str,
        line_count: int,
        checksum: str,
        content_expires_at: datetime,
    ) -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into delivery_files
                  (order_id, format, line_count, checksum_sha256, content, content_expires_at)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (order_id) do update set
                  format = excluded.format,
                  line_count = excluded.line_count,
                  checksum_sha256 = excluded.checksum_sha256,
                  content = excluded.content,
                  content_expires_at = excluded.content_expires_at
                """,
                (order_id, format, line_count, checksum, content, content_expires_at),
            )

    def _sync_extend_inventory(
        self,
        *,
        order_id: int,
        duration_days: int,
        inventory_ids: list[int] | None,
        geo_code: str | None,
    ) -> tuple[int, datetime | None]:
        """Bulk UPDATE proxy_inventory + propagate orders.proxies_expires_at."""
        with connect() as conn, conn.cursor() as cur:
            params: list[Any] = [duration_days, order_id]
            extra_where = ""
            if inventory_ids is not None:
                extra_where = " and id = any(%s)"
                params.append(inventory_ids)
            elif geo_code is not None:
                extra_where = " and geo_country = %s"
                params.append(geo_code)

            sql = f"""
                update proxy_inventory
                set expires_at = expires_at + (%s || ' days')::interval,
                    status = 'sold',
                    updated_at = now()
                where order_id = %s
                  and status in ('sold', 'expired_grace')
                  {extra_where}
                returning id, expires_at
            """
            cur.execute(sql, params)
            updated = list(cur.fetchall())

            if not updated:
                return 0, None

            new_max = max(r["expires_at"] for r in updated)

            cur.execute(
                """
                update orders
                set proxies_expires_at = (
                    select max(expires_at) from proxy_inventory
                    where order_id = %s and status in ('sold', 'expired_grace')
                ),
                updated_at = now()
                where id = %s
                """,
                (order_id, order_id),
            )
        return len(updated), new_max

    # === Redis idempotency ===

    async def _idem_get(self, key: str) -> ReserveResult | None:
        redis = await get_redis()
        cached = await redis.get(f"idem:reserve:{key}")
        if not cached:
            return None
        try:
            data = json.loads(cached)
        except json.JSONDecodeError:
            logger.warning("idem cache corrupt key=%s", key)
            return None
        expires_at_raw = data.get("expires_at")
        return ReserveResult(
            success=bool(data.get("success", False)),
            order_ref=data.get("order_ref"),
            expires_at=datetime.fromisoformat(expires_at_raw) if expires_at_raw else None,
            proxies_count=int(data.get("proxies_count", 0)),
            error=data.get("error"),
            available_now=data.get("available_now"),
        )

    async def _idem_set(self, key: str, result: ReserveResult) -> None:
        redis = await get_redis()
        payload = {
            "success": result.success,
            "order_ref": result.order_ref,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "proxies_count": result.proxies_count,
            "error": result.error,
            "available_now": result.available_now,
        }
        await redis.set(f"idem:reserve:{key}", json.dumps(payload), ex=_IDEM_CACHE_TTL_SEC)
