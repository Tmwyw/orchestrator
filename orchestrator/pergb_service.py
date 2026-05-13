"""Pay-per-GB business logic (Wave B-8.2).

Three async methods drive the reserve_pergb / topup_pergb / traffic endpoints.
Mirrors the AllocatorService shape — sync DB helpers wrapped via
``asyncio.to_thread``, Redis-backed idempotency with a UNIQUE-violation
fallback, structured logs.

Per design § 6:
- reserve_pergb auto-commits the order (status='committed', committed_at=now)
  because the user receives proxy creds immediately and the polling worker
  starts billing right away. No separate /commit step needed for pergb.
- topup_pergb creates a new committed order linked via metadata.parent_order_ref
  and atomically grows the parent's traffic_account quota + expires_at.
- get_traffic returns the per-account snapshot used by the bot's quota-poller.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from orchestrator import node_client
from orchestrator.api_schemas import SkuTierTable
from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.logging_setup import get_logger
from orchestrator.node_client import NodeAgentError
from orchestrator.redis_client import get_redis

logger = get_logger("netrun-orchestrator-pergb")

_GIB = 1024 * 1024 * 1024
_IDEM_CACHE_TTL_SEC = 24 * 60 * 60
_RESERVE_PERGB_IDEM_PREFIX = "idem:reserve_pergb:"
_TOPUP_PERGB_IDEM_PREFIX = "idem:topup_pergb:"
_GENERATE_PORTS_IDEM_PREFIX = "idem:generate_ports:"


# === Result dataclasses ===


@dataclass(slots=True)
class ReservePergbResult:
    """Wave PERGB-RFCT-A: no port credentials. Bot calls generate_ports
    after reserve to claim N pool ports lazily.
    """

    success: bool
    order_ref: str | None = None
    expires_at: datetime | None = None
    bytes_quota: int | None = None
    price_amount: Decimal | None = None
    traffic_account_id: int | None = None
    error: str | None = None
    available_tiers: list[int] | None = None


@dataclass(slots=True)
class GeneratedPortRow:
    port: int
    host: str
    login: str
    password: str
    geo_code: str


@dataclass(slots=True)
class GeneratePortsResult:
    success: bool
    order_ref: str | None = None
    traffic_account_id: int | None = None
    ports: list[GeneratedPortRow] | None = None
    total_ports_for_client: int | None = None
    error: str | None = None
    requested: int | None = None
    available: int | None = None
    geo_code: str | None = None
    current_status: str | None = None


@dataclass(slots=True)
class TopupPergbResult:
    success: bool
    order_ref: str | None = None
    parent_order_ref: str | None = None
    topup_sequence: int | None = None
    bytes_quota_total: int | None = None
    bytes_used: int | None = None
    expires_at: datetime | None = None
    price_amount: Decimal | None = None
    tier_price_per_gb: Decimal | None = None
    reactivated: bool = False
    error: str | None = None
    available_tiers: list[int] | None = None
    current_status: str | None = None


@dataclass(slots=True)
class TrafficResult:
    success: bool
    order_ref: str | None = None
    status: str | None = None
    bytes_quota: int | None = None
    bytes_used: int | None = None
    bytes_remaining: int | None = None
    usage_pct: float | None = None
    last_polled_at: datetime | None = None
    expires_at: datetime | None = None
    depleted_at: datetime | None = None
    node_id: str | None = None
    port: int | None = None
    over_usage_bytes: int = 0
    port_count: int = 0
    error: str | None = None
    detail: str | None = None


# === Service ===


class PergbService:
    """Pay-per-GB orchestrator-side business logic."""

    # ---------- reserve_pergb ----------

    async def reserve_pergb(
        self,
        *,
        user_id: int,
        sku_id: int,
        gb_amount: int,
        idempotency_key: str | None = None,
    ) -> ReservePergbResult:
        if idempotency_key:
            cached = await self._idem_get_reserve(idempotency_key)
            if cached is not None:
                logger.info(
                    "pergb_reserve_idempotent_hit",
                    idempotency_key=idempotency_key,
                    order_ref=cached.order_ref,
                )
                return cached

        sku = await asyncio.to_thread(self._sync_get_active_sku, sku_id)
        if sku is None:
            return ReservePergbResult(success=False, error="sku_not_found")
        if str(sku.get("product_kind") or "") != "datacenter_pergb":
            return ReservePergbResult(success=False, error="sku_not_pergb")

        try:
            tiers = SkuTierTable.model_validate(sku.get("metadata") or {})
        except Exception:
            return ReservePergbResult(success=False, error="sku_tiers_invalid")

        tier = next((t for t in tiers.tiers if t.gb == gb_amount), None)
        if tier is None:
            return ReservePergbResult(
                success=False,
                error="invalid_tier_amount",
                available_tiers=[t.gb for t in tiers.tiers],
            )

        bytes_quota = gb_amount * _GIB
        price_amount = (tier.price_per_gb * Decimal(gb_amount)).quantize(Decimal("0.00000001"))
        duration_days = int(sku.get("duration_days") or 30)
        now = datetime.now(timezone.utc)
        traffic_expires_at = now + timedelta(days=duration_days)
        proxies_expires_at = traffic_expires_at  # mirrors duration column

        created = await asyncio.to_thread(
            self._sync_create_pergb_account,
            user_id=user_id,
            sku_id=sku_id,
            gb_amount=gb_amount,
            price_amount=price_amount,
            tier_price_per_gb=tier.price_per_gb,
            bytes_quota=bytes_quota,
            duration_days=duration_days,
            traffic_expires_at=traffic_expires_at,
            proxies_expires_at=proxies_expires_at,
            idempotency_key=idempotency_key,
        )

        result = ReservePergbResult(
            success=True,
            order_ref=created["order_ref"],
            expires_at=created["proxies_expires_at"],
            bytes_quota=bytes_quota,
            price_amount=price_amount,
            traffic_account_id=int(created["traffic_account_id"]),
        )

        if idempotency_key:
            await self._idem_set_reserve(idempotency_key, result)

        logger.info(
            "pergb_reserve_succeeded",
            order_ref=result.order_ref,
            sku_id=sku_id,
            user_id=user_id,
            gb_amount=gb_amount,
            bytes_quota=bytes_quota,
            traffic_account_id=int(created["traffic_account_id"]),
        )
        return result

    # ---------- generate_ports ----------

    async def generate_ports(
        self,
        *,
        order_ref: str,
        count: int,
        geo_code: str,
        idempotency_key: str,
    ) -> GeneratePortsResult:
        """Allocate N pool ports for an existing pergb traffic_account.

        Wave PERGB-RFCT-A: replaces the legacy 1:1 port allocation that
        used to happen inside reserve_pergb. The user buys a GB budget
        first, then explicitly opts in to "generate proxies" — picking
        geo + count — and we claim from the shared pool atomically.
        """
        cached = await self._idem_get_generate(idempotency_key)
        if cached is not None:
            logger.info(
                "pergb_generate_idempotent_hit",
                idempotency_key=idempotency_key,
                order_ref=cached.order_ref,
            )
            return cached

        # Fetch & validate the parent account.
        ta = await asyncio.to_thread(self._sync_get_pergb_parent, order_ref)
        if ta is None:
            return GeneratePortsResult(success=False, error="order_not_found")
        status = str(ta.get("account_status") or "")
        if status != "active":
            return GeneratePortsResult(
                success=False,
                error="account_not_active",
                current_status=status or None,
            )

        traffic_account_id = int(ta["account_id"])

        rows = await asyncio.to_thread(
            self._sync_atomic_allocate_ports,
            traffic_account_id=traffic_account_id,
            order_ref=order_ref,
            geo_code=geo_code,
            count=count,
        )
        if rows is None:
            # Probe how many pool ports are available so the bot can show a
            # useful "X available, Y requested" error.
            available = await asyncio.to_thread(self._sync_count_available_pool_ports, geo_code=geo_code)
            return GeneratePortsResult(
                success=False,
                error="insufficient_pool",
                requested=count,
                available=available,
                geo_code=geo_code,
            )

        # Best-effort node-side activation. Failures are logged + watchdog
        # retries via existing safety-net (Phase 5.5) — we don't roll back.
        for r in rows:
            await asyncio.to_thread(
                self._best_effort_post_enable_one,
                node_id=str(r["node_id"]),
                port=int(r["port"]),
            )

        total = await asyncio.to_thread(self._sync_count_linked_ports, traffic_account_id=traffic_account_id)

        result = GeneratePortsResult(
            success=True,
            order_ref=order_ref,
            traffic_account_id=traffic_account_id,
            ports=[
                GeneratedPortRow(
                    port=int(r["port"]),
                    host=str(r["host"]),
                    login=str(r["login"]),
                    password=str(r["password"]),
                    geo_code=str(r["geo_code"]),
                )
                for r in rows
            ],
            total_ports_for_client=total,
        )
        await self._idem_set_generate(idempotency_key, result)

        logger.info(
            "pergb_generate_succeeded",
            order_ref=order_ref,
            traffic_account_id=traffic_account_id,
            count=count,
            geo_code=geo_code,
            total_ports_for_client=total,
        )
        return result

    # ---------- topup_pergb ----------

    async def topup_pergb(
        self,
        *,
        parent_order_ref: str,
        sku_id: int,
        gb_amount: int,
        idempotency_key: str | None = None,
    ) -> TopupPergbResult:
        if idempotency_key:
            cached = await self._idem_get_topup(idempotency_key)
            if cached is not None:
                logger.info(
                    "pergb_topup_idempotent_hit",
                    idempotency_key=idempotency_key,
                    order_ref=cached.order_ref,
                )
                return cached

        parent = await asyncio.to_thread(self._sync_get_pergb_parent, parent_order_ref)
        if parent is None:
            return TopupPergbResult(success=False, error="order_not_found")
        if int(parent["sku_id"]) != int(sku_id):
            return TopupPergbResult(success=False, error="sku_mismatch_for_topup")

        # Lookup SKU + traffic_account
        sku = await asyncio.to_thread(self._sync_get_active_sku, int(parent["sku_id"]))
        if sku is None:
            return TopupPergbResult(success=False, error="sku_not_found")

        try:
            tiers = SkuTierTable.model_validate(sku.get("metadata") or {})
        except Exception:
            return TopupPergbResult(success=False, error="sku_tiers_invalid")

        tier = next((t for t in tiers.tiers if t.gb == gb_amount), None)
        if tier is None:
            return TopupPergbResult(
                success=False,
                error="invalid_tier_amount",
                available_tiers=[t.gb for t in tiers.tiers],
            )

        account_status = str(parent.get("account_status") or "")
        if account_status not in ("active", "depleted"):
            return TopupPergbResult(
                success=False,
                error="account_not_renewable",
                current_status=account_status or None,
            )

        bytes_added = gb_amount * _GIB
        price_amount = (tier.price_per_gb * Decimal(gb_amount)).quantize(Decimal("0.00000001"))
        duration_days = int(sku.get("duration_days") or 30)

        outcome = await asyncio.to_thread(
            self._sync_apply_topup,
            parent_order_id=int(parent["order_id"]),
            parent_order_ref=parent_order_ref,
            account_id=int(parent["account_id"]),
            user_id=int(parent["user_id"]),
            sku_id=int(parent["sku_id"]),
            gb_amount=gb_amount,
            bytes_added=bytes_added,
            price_amount=price_amount,
            tier_price_per_gb=tier.price_per_gb,
            duration_days=duration_days,
            idempotency_key=idempotency_key,
        )
        if outcome.get("error") == "duplicate_idempotency_key":
            # UNIQUE-violation Path B (D6.4) — fetch and return existing top-up's response
            existing = outcome.get("existing")
            assert existing is not None
            return self._result_from_existing_topup(existing)

        # Reactivation: if account flipped depleted → active, fan out
        # post_enable across all linked ports (Wave PERGB-RFCT-A: 1 → N).
        if outcome["reactivated"]:
            await asyncio.to_thread(
                self._best_effort_post_enable_all,
                account_id=int(parent["account_id"]),
            )

        result = TopupPergbResult(
            success=True,
            order_ref=outcome["new_order_ref"],
            parent_order_ref=parent_order_ref,
            topup_sequence=int(outcome["topup_sequence"]),
            bytes_quota_total=int(outcome["bytes_quota_total"]),
            bytes_used=int(outcome["bytes_used"]),
            expires_at=outcome["new_expires_at"],
            price_amount=price_amount,
            tier_price_per_gb=tier.price_per_gb,
            reactivated=bool(outcome["reactivated"]),
        )

        if idempotency_key:
            await self._idem_set_topup(idempotency_key, result)

        logger.info(
            "pergb_topup_succeeded",
            order_ref=result.order_ref,
            parent_order_ref=parent_order_ref,
            gb_amount=gb_amount,
            reactivated=result.reactivated,
        )
        return result

    # ---------- get_traffic ----------

    async def get_traffic(self, *, parent_order_ref: str) -> TrafficResult:
        snapshot = await asyncio.to_thread(self._sync_get_traffic_snapshot, parent_order_ref)
        if snapshot is None:
            return TrafficResult(success=False, error="order_not_found")
        if not snapshot.get("has_account"):
            return TrafficResult(
                success=False,
                error="traffic_account_not_found",
                detail=(
                    "this is a top-up order; use the parent order_ref"
                    if snapshot.get("is_topup")
                    else "no traffic_account is associated with this order"
                ),
            )

        bytes_quota = int(snapshot["bytes_quota"])
        bytes_used = int(snapshot["bytes_used"])
        bytes_remaining = max(0, bytes_quota - bytes_used)
        over_usage = max(0, bytes_used - bytes_quota)
        usage_pct = 0.0 if bytes_quota == 0 else min(1.0, bytes_used / bytes_quota)
        return TrafficResult(
            success=True,
            order_ref=parent_order_ref,
            status=str(snapshot["status"]),
            bytes_quota=bytes_quota,
            bytes_used=bytes_used,
            bytes_remaining=bytes_remaining,
            usage_pct=usage_pct,
            last_polled_at=snapshot.get("last_polled_at"),
            expires_at=snapshot["expires_at"],
            depleted_at=snapshot.get("depleted_at"),
            node_id=(str(snapshot["node_id"]) if snapshot.get("node_id") else None),
            port=(int(snapshot["port"]) if snapshot.get("port") is not None else None),
            over_usage_bytes=over_usage,
            port_count=int(snapshot.get("port_count") or 0),
        )

    # ===========================================================
    # Sync DB helpers (run inside asyncio.to_thread).
    # ===========================================================

    def _sync_get_active_sku(self, sku_id: int) -> dict[str, Any] | None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select * from skus where id = %s and is_active = true",
                (sku_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def _sync_create_pergb_account(
        self,
        *,
        user_id: int,
        sku_id: int,
        gb_amount: int,
        price_amount: Decimal,
        tier_price_per_gb: Decimal,
        bytes_quota: int,
        duration_days: int,
        traffic_expires_at: datetime,
        proxies_expires_at: datetime,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """One transaction: insert order + traffic_account (no inventory claim).

        Wave PERGB-RFCT-A: reserve_pergb only creates the GB budget. The user
        then calls /v1/pergb/{order_ref}/generate_ports to claim N pool ports
        lazily. traffic_accounts.inventory_id is left NULL — the reverse FK on
        proxy_inventory.traffic_account_id is the new source of truth for the
        account ↔ ports relationship.
        """
        with connect() as conn, conn.cursor() as cur:
            # Wave PERGB-INFINITE: pull a clean sequential id from
            # `order_ref_seq` (migration 029) instead of the legacy
            # `ord_<hex>` shape. Generation lives inside the same TX so
            # the sequence value commits atomically with the order row.
            cur.execute("SELECT nextval('order_ref_seq')")
            _seq_row = cur.fetchone()
            assert _seq_row is not None
            _seq_value = _seq_row[0] if not isinstance(_seq_row, dict) else next(iter(_seq_row.values()))
            order_ref = f"order_{int(_seq_value)}"
            # 1. Insert order (auto-committed for pergb — see module doc)
            metadata = {
                "chosen_tier_gb": gb_amount,
                "tier_price_per_gb": str(tier_price_per_gb),
                "bytes_quota": bytes_quota,
                "duration_days": duration_days,
            }
            cur.execute(
                """
                insert into orders (
                  order_ref, user_id, sku_id, status,
                  requested_count, allocated_count,
                  reservation_key, reserved_at, expires_at,
                  committed_at, proxies_expires_at,
                  price_amount, idempotency_key, metadata
                )
                values (
                  %s, %s, %s, 'committed',
                  0, 0,
                  %s, now(), %s,
                  now(), %s,
                  %s, %s, %s
                )
                returning id
                """,
                (
                    order_ref,
                    user_id,
                    sku_id,
                    f"resv_pergb_{uuid.uuid4().hex}",
                    proxies_expires_at,
                    proxies_expires_at,
                    str(price_amount),
                    idempotency_key,
                    Jsonb(metadata),
                ),
            )
            order_row = cur.fetchone()
            assert order_row is not None
            order_id = int(order_row["id"])

            # 2. Insert traffic_account (inventory_id NULL — ports come later)
            cur.execute(
                """
                insert into traffic_accounts (
                  order_id, inventory_id, bytes_quota, bytes_used,
                  status, expires_at
                )
                values (%s, NULL, %s, 0, 'active', %s)
                returning id
                """,
                (order_id, bytes_quota, traffic_expires_at),
            )
            ta_row = cur.fetchone()
            assert ta_row is not None
            traffic_account_id = int(ta_row["id"])

        return {
            "order_ref": order_ref,
            "order_id": order_id,
            "traffic_account_id": traffic_account_id,
            "proxies_expires_at": proxies_expires_at,
        }

    def _sync_atomic_allocate_ports(
        self,
        *,
        traffic_account_id: int,
        order_ref: str,
        geo_code: str,
        count: int,
    ) -> list[dict[str, Any]] | None:
        """Atomically claim N available pool ports of the requested geo and
        link them to the traffic_account. Returns the allocated rows, or
        ``None`` if fewer than ``count`` were available.

        Single-statement UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP
        LOCKED) is race-free across concurrent generate_ports calls.
        """
        reservation_key = f"resv_pergb_gen_{uuid.uuid4().hex}"
        # NOTE: ``proxy_inventory.geo_country`` is nullable + stores full
        # country names ('Japan', 'India'), not the ISO codes the API/bot
        # use. The canonical link is via ``proxy_inventory.sku_id`` →
        # ``skus.geo_code`` (same path ``list_active_skus`` takes). We
        # filter through that JOIN so the pergb pool view matches the
        # IPv6 catalog the user sees.
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                with selected as (
                    select pi.id
                    from proxy_inventory pi
                    join skus s on s.id = pi.sku_id
                    where pi.status = 'available'
                      and s.is_active = true
                      and s.product_kind = 'ipv6'
                      and s.geo_code = %s
                    order by pi.id
                    for update skip locked
                    limit %s
                ), updated as (
                    update proxy_inventory
                    set status = 'allocated_pergb',
                        traffic_account_id = %s,
                        reservation_key = %s,
                        reserved_at = now(),
                        sold_at = now(),
                        updated_at = now()
                    where id in (select id from selected)
                    returning id, node_id, port, host, login, password, sku_id
                )
                select u.id, u.node_id, u.port, u.host, u.login, u.password,
                       s.geo_code
                from updated u
                join skus s on s.id = u.sku_id
                """,
                (geo_code, count, traffic_account_id, reservation_key),
            )
            rows = [dict(r) for r in cur.fetchall()]
            if len(rows) < count:
                # Insufficient inventory — roll back the partial allocation.
                conn.rollback()
                return None
        return rows

    def _sync_count_available_pool_ports(self, *, geo_code: str) -> int:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select count(*) as c
                from proxy_inventory pi
                join skus s on s.id = pi.sku_id
                where pi.status = 'available'
                  and s.is_active = true
                  and s.product_kind = 'ipv6'
                  and s.geo_code = %s
                """,
                (geo_code,),
            )
            row = cur.fetchone() or {"c": 0}
        return int(row.get("c") or 0)

    def _best_effort_post_enable_one(self, *, node_id: str, port: int) -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select url, api_key from nodes where id = %s",
                (node_id,),
            )
            row = cur.fetchone()
        if row is None:
            logger.warning("pergb_generate_node_lookup_failed", node_id=node_id, port=port)
            return
        node_url = str(row["url"])
        node_api_key = str(row["api_key"]) if row.get("api_key") else None
        try:
            node_client.post_enable(node_url, node_api_key, port)
            logger.info("pergb_generate_port_enabled", node_id=node_id, port=port)
        except NodeAgentError as exc:
            logger.warning(
                "pergb_generate_port_enable_failed",
                node_id=node_id,
                port=port,
                error=str(exc),
                status_code=exc.status_code,
            )

    def _sync_count_linked_ports(self, *, traffic_account_id: int) -> int:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                "select count(*) as c from proxy_inventory where traffic_account_id = %s",
                (traffic_account_id,),
            )
            row = cur.fetchone() or {"c": 0}
        return int(row.get("c") or 0)

    # ---------- list_active_ports (re-download support) ----------

    async def list_active_ports(self, *, order_ref: str) -> list[GeneratedPortRow] | None:
        """Return ALL ports currently claimed under a pergb order's
        traffic_account. Used by the bot's «Скачать все мои прокси»
        button so the user can re-fetch the .txt of every generation
        batch they've ever made on this order.

        Returns None if the order_ref doesn't exist / isn't pergb /
        has no traffic_account. Empty list if the account exists but
        no ports have been generated yet.
        """
        parent = await asyncio.to_thread(self._sync_get_pergb_parent, order_ref)
        if parent is None:
            return None
        traffic_account_id = int(parent["account_id"])

        rows = await asyncio.to_thread(
            self._sync_list_pergb_account_ports,
            traffic_account_id=traffic_account_id,
        )
        return [
            GeneratedPortRow(
                port=int(r["port"]),
                host=str(r["host"]),
                login=str(r["login"]),
                password=str(r["password"]),
                geo_code=str(r["geo_code"] or ""),
            )
            for r in rows
        ]

    def _sync_list_pergb_account_ports(self, *, traffic_account_id: int) -> list[dict[str, Any]]:
        """All currently-claimed proxy_inventory rows under this pergb
        account, with host/login/password + geo for .txt rendering.

        Only returns rows in 'allocated_pergb' status — excludes ports
        that were released/expired. Sorted by port for stable output."""
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select pi.port, pi.host, pi.login, pi.password,
                       s.geo_code
                from proxy_inventory pi
                join skus s on s.id = pi.sku_id
                where pi.traffic_account_id = %s
                  and pi.status = 'allocated_pergb'
                order by pi.port
                """,
                (traffic_account_id,),
            )
            return [dict(r) for r in cur.fetchall()]

    def _sync_get_pergb_parent(self, parent_order_ref: str) -> dict[str, Any] | None:
        """Fetch the parent reserve_pergb order + its traffic_account.

        Wave PERGB-RFCT-A: no inventory join — traffic_accounts.inventory_id
        is NULL for new orders. Linked ports (for topup reactivation) come
        from a separate _sync_get_linked_ports call so we can fan out across
        N ports.

        Returns None if order_ref doesn't exist or has no traffic_account
        (e.g. it's a top-up's order_ref or a per-piece order).
        """
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select
                  o.id            as order_id,
                  o.order_ref     as order_ref,
                  o.user_id       as user_id,
                  o.sku_id        as sku_id,
                  t.id            as account_id,
                  t.status        as account_status,
                  t.bytes_quota   as bytes_quota,
                  t.bytes_used    as bytes_used,
                  t.expires_at    as expires_at
                from orders o
                join traffic_accounts t on t.order_id = o.id
                where o.order_ref = %s
                """,
                (parent_order_ref,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def _sync_get_linked_ports(self, *, account_id: int) -> list[dict[str, Any]]:
        """Return all proxy_inventory rows currently linked to the given
        traffic_account, plus node URL/API key for post_enable/post_disable
        fan-out. Empty list if no ports allocated yet (initial state).
        """
        rows: list[dict[str, Any]] = []
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select i.id as inventory_id, i.node_id, i.port,
                       n.url as node_url, n.api_key as node_api_key
                  from proxy_inventory i
                  join nodes n on n.id = i.node_id
                 where i.traffic_account_id = %s
                """,
                (account_id,),
            )
            for r in cur.fetchall():
                rows.append(
                    {
                        "inventory_id": int(r["inventory_id"]),
                        "node_id": str(r["node_id"]),
                        "port": int(r["port"]),
                        "node_url": str(r["node_url"]),
                        "node_api_key": (str(r["node_api_key"]) if r.get("node_api_key") else None),
                    }
                )
        return rows

    def _sync_apply_topup(
        self,
        *,
        parent_order_id: int,
        parent_order_ref: str,
        account_id: int,
        user_id: int,
        sku_id: int,
        gb_amount: int,
        bytes_added: int,
        price_amount: Decimal,
        tier_price_per_gb: Decimal,
        duration_days: int,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """One transaction: insert new top-up order, update traffic_account.

        Handles UNIQUE-violation on idempotency_key per design § 6.3 D6.4
        Path B (return existing).
        """
        # Wave PERGB-INFINITE: top-up orders share the same `order_<N>`
        # sequence as fresh reservations (migration 029). The `topup_sequence`
        # in metadata still tracks per-parent ordering for billing audits.
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT nextval('order_ref_seq')")
            _seq_row = cur.fetchone()
            assert _seq_row is not None
            _seq_value = _seq_row[0] if not isinstance(_seq_row, dict) else next(iter(_seq_row.values()))
            new_order_ref = f"order_{int(_seq_value)}"
            cur.execute(
                """
                select count(*) as c
                from orders
                where (metadata ->> 'parent_order_ref') = %s
                """,
                (parent_order_ref,),
            )
            row = cur.fetchone() or {"c": 0}
            topup_sequence = int(row.get("c") or 0) + 1

            metadata = {
                "parent_order_ref": parent_order_ref,
                "topup_sequence": topup_sequence,
                "chosen_tier_gb": gb_amount,
                "tier_price_per_gb": str(tier_price_per_gb),
                "bytes_added": bytes_added,
            }
            try:
                cur.execute(
                    """
                    insert into orders (
                      order_ref, user_id, sku_id, status,
                      requested_count, allocated_count,
                      reservation_key, reserved_at, expires_at,
                      committed_at, proxies_expires_at,
                      price_amount, idempotency_key, metadata
                    )
                    values (
                      %s, %s, %s, 'committed',
                      1, 1,
                      %s, now(), now() + (%s || ' seconds')::interval,
                      now(), now() + (%s || ' days')::interval,
                      %s, %s, %s
                    )
                    returning id
                    """,
                    (
                        new_order_ref,
                        user_id,
                        sku_id,
                        f"topup_{secrets.token_hex(8)}",
                        get_config().reservation_default_ttl_sec,
                        duration_days,
                        str(price_amount),
                        idempotency_key,
                        Jsonb(metadata),
                    ),
                )
            except psycopg.errors.UniqueViolation:
                # Path B: idempotency_key already used. Fetch the existing top-up
                # and let the caller return its cached response shape.
                conn.rollback()
                return {
                    "error": "duplicate_idempotency_key",
                    "existing": self._sync_fetch_topup_by_idem(idempotency_key),
                }

            new_order_row = cur.fetchone()
            assert new_order_row is not None

            # Atomic UPDATE traffic_account: quota grows, expires_at = MAX(curr, now+duration),
            # status flips depleted → active iff bytes_used < new_quota.
            cur.execute(
                """
                update traffic_accounts
                set bytes_quota = bytes_quota + %s,
                    expires_at = greatest(expires_at, now() + (%s || ' days')::interval),
                    status = case
                      when status = 'depleted' and bytes_used < (bytes_quota + %s) then 'active'
                      else status
                    end,
                    updated_at = now()
                where id = %s
                returning bytes_quota, bytes_used, expires_at, status,
                  case when status = 'active' and depleted_at is not null then true else false end as just_reactivated
                """,
                (bytes_added, duration_days, bytes_added, account_id),
            )
            updated_row = cur.fetchone()
            assert updated_row is not None
            updated = dict(updated_row)

            # Clear depleted_at when reactivated for cleaner downstream queries.
            reactivated = bool(updated.get("just_reactivated"))
            if reactivated:
                cur.execute(
                    "update traffic_accounts set depleted_at = null, updated_at = now() where id = %s",
                    (account_id,),
                )

        return {
            "new_order_ref": new_order_ref,
            "topup_sequence": topup_sequence,
            "bytes_quota_total": int(updated["bytes_quota"]),
            "bytes_used": int(updated["bytes_used"]),
            "new_expires_at": updated["expires_at"],
            "reactivated": reactivated,
        }

    def _sync_fetch_topup_by_idem(self, idempotency_key: str | None) -> dict[str, Any] | None:
        if not idempotency_key:
            return None
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select o.order_ref, o.metadata, o.price_amount, o.proxies_expires_at,
                       t.bytes_quota, t.bytes_used
                from orders o
                join traffic_accounts t on t.order_id = (
                    select id from orders where order_ref = (o.metadata ->> 'parent_order_ref')
                )
                where o.idempotency_key = %s
                """,
                (idempotency_key,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def _result_from_existing_topup(self, row: dict[str, Any] | None) -> TopupPergbResult:
        if row is None:
            # Should not happen — UNIQUE was raised so the row exists. Defensive
            # fallback: surface a generic conflict to the caller.
            return TopupPergbResult(success=False, error="duplicate_idempotency_key")
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        tier_price = meta.get("tier_price_per_gb")
        return TopupPergbResult(
            success=True,
            order_ref=str(row["order_ref"]),
            parent_order_ref=str(meta.get("parent_order_ref") or ""),
            topup_sequence=int(meta.get("topup_sequence") or 0),
            bytes_quota_total=int(row["bytes_quota"]),
            bytes_used=int(row["bytes_used"]),
            expires_at=row["proxies_expires_at"],
            price_amount=Decimal(str(row["price_amount"])) if row.get("price_amount") is not None else None,
            tier_price_per_gb=Decimal(str(tier_price)) if tier_price is not None else None,
            reactivated=False,
        )

    def _sync_get_traffic_snapshot(self, parent_order_ref: str) -> dict[str, Any] | None:
        """Snapshot for /v1/orders/{order_ref}/traffic.

        Wave PERGB-RFCT-A: node_id/port no longer come from a single
        inventory row. We pick a representative linked port via LATERAL
        (any one is fine for the bot's status display); the account-level
        bytes_used is still authoritative because traffic_poll aggregates
        SUM(bytes_used_snapshot) into traffic_accounts.bytes_used per
        cycle. Returns has_account=True even when no ports are allocated
        yet — the bot's pergb panel renders the "Generate ports" CTA in
        that case.
        """
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select o.id, o.metadata, t.id as account_id,
                       t.status, t.bytes_quota, t.bytes_used,
                       t.last_polled_at, t.expires_at, t.depleted_at,
                       (select i.node_id from proxy_inventory i
                          where i.traffic_account_id = t.id
                          order by i.id limit 1) as node_id,
                       (select i.port from proxy_inventory i
                          where i.traffic_account_id = t.id
                          order by i.id limit 1) as port,
                       (select count(*) from proxy_inventory i
                          where i.traffic_account_id = t.id) as port_count
                from orders o
                left join traffic_accounts t on t.order_id = o.id
                where o.order_ref = %s
                """,
                (parent_order_ref,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        meta = row.get("metadata") or {}
        if isinstance(meta, str):
            meta = json.loads(meta)
        is_topup = bool(meta.get("parent_order_ref"))
        if row.get("account_id") is None:
            return {"has_account": False, "is_topup": is_topup}
        return {
            "has_account": True,
            "status": str(row["status"]),
            "bytes_quota": int(row["bytes_quota"]),
            "bytes_used": int(row["bytes_used"]),
            "last_polled_at": row.get("last_polled_at"),
            "expires_at": row["expires_at"],
            "depleted_at": row.get("depleted_at"),
            "node_id": (str(row["node_id"]) if row.get("node_id") else None),
            "port": (int(row["port"]) if row.get("port") is not None else None),
            "port_count": int(row.get("port_count") or 0),
        }

    def _best_effort_post_enable_all(self, *, account_id: int) -> None:
        """Fan out post_enable across every port linked to this account.

        Reactivation succeeds (in DB terms) iff *all* ports ack — partial
        failures leave node_blocked=TRUE for watchdog retry, but the
        traffic_account itself stays active so billing resumes.
        """
        ports = self._sync_get_linked_ports(account_id=account_id)
        if not ports:
            # No ports yet — first generate_ports call will activate them.
            return
        all_ok = True
        for p in ports:
            try:
                node_client.post_enable(p["node_url"], p["node_api_key"], p["port"])
                logger.info(
                    "pergb_account_reactivated_port",
                    account_id=account_id,
                    node_id=p["node_id"],
                    port=p["port"],
                )
            except NodeAgentError as exc:
                all_ok = False
                logger.warning(
                    "pergb_account_reactivate_failed",
                    account_id=account_id,
                    node_id=p["node_id"],
                    port=p["port"],
                    error=str(exc),
                    status_code=exc.status_code,
                )
        # Stamp attempt time unconditionally; clear node_blocked only when
        # every linked port acked.
        with connect() as conn, conn.cursor() as cur:
            if all_ok:
                cur.execute(
                    "update traffic_accounts "
                    "set node_blocked = FALSE, "
                    "    last_unblock_attempt_at = now(), "
                    "    updated_at = now() "
                    "where id = %s",
                    (account_id,),
                )
            else:
                cur.execute(
                    "update traffic_accounts "
                    "set last_unblock_attempt_at = now(), "
                    "    updated_at = now() "
                    "where id = %s",
                    (account_id,),
                )

    # ===========================================================
    # Redis idempotency helpers
    # ===========================================================

    async def _idem_get_reserve(self, key: str) -> ReservePergbResult | None:
        redis = await get_redis()
        cached = await redis.get(_RESERVE_PERGB_IDEM_PREFIX + key)
        if not cached:
            return None
        try:
            data = json.loads(cached)
        except json.JSONDecodeError:
            logger.warning("pergb_reserve_idem_corrupt", key=key)
            return None
        return ReservePergbResult(
            success=bool(data.get("success", False)),
            order_ref=data.get("order_ref"),
            expires_at=(datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None),
            bytes_quota=data.get("bytes_quota"),
            price_amount=Decimal(data["price_amount"]) if data.get("price_amount") else None,
            traffic_account_id=data.get("traffic_account_id"),
            error=data.get("error"),
            available_tiers=data.get("available_tiers"),
        )

    async def _idem_set_reserve(self, key: str, result: ReservePergbResult) -> None:
        redis = await get_redis()
        payload = {
            "success": result.success,
            "order_ref": result.order_ref,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "bytes_quota": result.bytes_quota,
            "price_amount": str(result.price_amount) if result.price_amount else None,
            "traffic_account_id": result.traffic_account_id,
            "error": result.error,
            "available_tiers": result.available_tiers,
        }
        await redis.set(_RESERVE_PERGB_IDEM_PREFIX + key, json.dumps(payload), ex=_IDEM_CACHE_TTL_SEC)

    async def _idem_get_generate(self, key: str) -> GeneratePortsResult | None:
        redis = await get_redis()
        cached = await redis.get(_GENERATE_PORTS_IDEM_PREFIX + key)
        if not cached:
            return None
        try:
            data = json.loads(cached)
        except json.JSONDecodeError:
            logger.warning("pergb_generate_idem_corrupt", key=key)
            return None
        ports_raw = data.get("ports") or []
        ports = [
            GeneratedPortRow(
                port=int(p["port"]),
                host=str(p["host"]),
                login=str(p["login"]),
                password=str(p["password"]),
                geo_code=str(p["geo_code"]),
            )
            for p in ports_raw
        ]
        return GeneratePortsResult(
            success=bool(data.get("success", False)),
            order_ref=data.get("order_ref"),
            traffic_account_id=data.get("traffic_account_id"),
            ports=ports if ports else None,
            total_ports_for_client=data.get("total_ports_for_client"),
            error=data.get("error"),
            requested=data.get("requested"),
            available=data.get("available"),
            geo_code=data.get("geo_code"),
            current_status=data.get("current_status"),
        )

    async def _idem_set_generate(self, key: str, result: GeneratePortsResult) -> None:
        redis = await get_redis()
        payload = {
            "success": result.success,
            "order_ref": result.order_ref,
            "traffic_account_id": result.traffic_account_id,
            "ports": [
                {
                    "port": p.port,
                    "host": p.host,
                    "login": p.login,
                    "password": p.password,
                    "geo_code": p.geo_code,
                }
                for p in (result.ports or [])
            ],
            "total_ports_for_client": result.total_ports_for_client,
            "error": result.error,
            "requested": result.requested,
            "available": result.available,
            "geo_code": result.geo_code,
            "current_status": result.current_status,
        }
        await redis.set(_GENERATE_PORTS_IDEM_PREFIX + key, json.dumps(payload), ex=_IDEM_CACHE_TTL_SEC)

    async def _idem_get_topup(self, key: str) -> TopupPergbResult | None:
        redis = await get_redis()
        cached = await redis.get(_TOPUP_PERGB_IDEM_PREFIX + key)
        if not cached:
            return None
        try:
            data = json.loads(cached)
        except json.JSONDecodeError:
            logger.warning("pergb_topup_idem_corrupt", key=key)
            return None
        return TopupPergbResult(
            success=bool(data.get("success", False)),
            order_ref=data.get("order_ref"),
            parent_order_ref=data.get("parent_order_ref"),
            topup_sequence=data.get("topup_sequence"),
            bytes_quota_total=data.get("bytes_quota_total"),
            bytes_used=data.get("bytes_used"),
            expires_at=(datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None),
            price_amount=Decimal(data["price_amount"]) if data.get("price_amount") else None,
            tier_price_per_gb=(Decimal(data["tier_price_per_gb"]) if data.get("tier_price_per_gb") else None),
            reactivated=bool(data.get("reactivated", False)),
            error=data.get("error"),
            available_tiers=data.get("available_tiers"),
            current_status=data.get("current_status"),
        )

    async def _idem_set_topup(self, key: str, result: TopupPergbResult) -> None:
        redis = await get_redis()
        payload = {
            "success": result.success,
            "order_ref": result.order_ref,
            "parent_order_ref": result.parent_order_ref,
            "topup_sequence": result.topup_sequence,
            "bytes_quota_total": result.bytes_quota_total,
            "bytes_used": result.bytes_used,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "price_amount": str(result.price_amount) if result.price_amount else None,
            "tier_price_per_gb": str(result.tier_price_per_gb) if result.tier_price_per_gb else None,
            "reactivated": result.reactivated,
            "error": result.error,
            "available_tiers": result.available_tiers,
            "current_status": result.current_status,
        }
        await redis.set(_TOPUP_PERGB_IDEM_PREFIX + key, json.dumps(payload), ex=_IDEM_CACHE_TTL_SEC)
