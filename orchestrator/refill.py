"""Refill engine: keeps proxy_inventory at target_stock per SKU."""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb

from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.distribution import equal_share
from orchestrator.jobs import allocate_port_range_via_table, log_job_event
from orchestrator.logging_setup import get_logger
from shared.contracts import PRODUCTION_PROFILE

logger = get_logger("netrun-orchestrator-refill")

# Map SKU.product_kind to the legacy "product" string the generation worker
# already understands (used as job.product). Pay-per-GB SKUs (Wave B-8) will
# need a different product string when their worker path lands.
_PRODUCT_BY_KIND: dict[str, str] = {
    "ipv6": "android_ipv6_only",
    "datacenter_pergb": "datacenter_pergb",
}


class RefillService:
    """One-pass refill engine.

    ``run_once()`` iterates active SKUs, computes deficit, distributes work
    across active bindings via :func:`equal_share`, and enqueues generation
    jobs (status='queued', reason='refill') with port ranges reserved through
    ``node_port_allocations``.
    """

    def run_once(self) -> dict[str, int]:
        """Single refill pass.

        Returns a counters dict::

            {
                "skus_processed": int,
                "skus_with_deficit": int,
                "jobs_enqueued": int,
                "nodes_at_capacity": int,
            }
        """
        counters = {
            "skus_processed": 0,
            "skus_with_deficit": 0,
            "jobs_enqueued": 0,
            "nodes_at_capacity": 0,
        }
        cfg = get_config()

        with connect() as conn:
            skus = self._list_active_skus(conn, limit=cfg.refill_max_skus_per_cycle)
            for sku in skus:
                counters["skus_processed"] += 1

                projection = self._get_sku_projection(conn, int(sku["id"]))
                available = int(projection["available"])
                deficit = int(sku["target_stock"]) - available
                if deficit <= 0:
                    continue

                counters["skus_with_deficit"] += 1
                to_schedule = min(deficit, int(sku["refill_batch_size"]))

                bindings = self._list_active_bindings_with_capacity(
                    conn,
                    sku_id=int(sku["id"]),
                    allow_degraded=cfg.proxy_allow_degraded_nodes,
                )
                if not bindings:
                    logger.info(
                        "refill_sku_skipped",
                        sku=sku["code"],
                        reason="no_active_bindings",
                    )
                    continue

                caps = [int(b["effective_max_batch"]) for b in bindings]
                distribution = equal_share(to_schedule, caps)

                for binding, qty in zip(bindings, distribution, strict=True):
                    if qty <= 0:
                        continue

                    in_flight = self._count_in_flight_jobs(conn, node_id=binding["node_id"])
                    if in_flight >= int(binding["max_parallel_jobs"]):
                        counters["nodes_at_capacity"] += 1
                        logger.info(
                            "refill_node_skipped",
                            node_id=binding["node_id"],
                            sku=sku["code"],
                            in_flight=in_flight,
                            max_parallel=binding["max_parallel_jobs"],
                            reason="in_flight_at_capacity",
                        )
                        continue

                    job_id = str(uuid.uuid4())
                    payload = self._build_refill_payload(sku=sku, count=qty)
                    job_inserted = False
                    try:
                        self._insert_refill_job(
                            conn,
                            job_id=job_id,
                            sku_id=int(sku["id"]),
                            node_id=str(binding["node_id"]),
                            count=qty,
                            priority=cfg.refill_default_priority,
                            product=_PRODUCT_BY_KIND.get(str(sku["product_kind"]), str(sku["code"])),
                            payload=payload,
                        )
                        job_inserted = True
                        start_port, _ = allocate_port_range_via_table(
                            conn,
                            node_id=str(binding["node_id"]),
                            job_id=job_id,
                            count=qty,
                        )
                        self._set_job_start_port(conn, job_id=job_id, start_port=start_port)

                        counters["jobs_enqueued"] += 1
                        logger.info(
                            "refill_job_enqueued",
                            job_id=job_id,
                            sku=sku["code"],
                            node_id=binding["node_id"],
                            count=qty,
                            start_port=start_port,
                        )
                    except Exception as exc:
                        if job_inserted:
                            try:
                                log_job_event(
                                    conn,
                                    job_id,
                                    "refill_failed",
                                    {
                                        "error": str(exc),
                                        "error_type": type(exc).__name__,
                                        "sku_id": int(sku["id"]),
                                        "node_id": str(binding["node_id"]),
                                        "count": qty,
                                    },
                                )
                            except Exception:
                                logger.exception(
                                    "refill_log_job_event_failed", job_id=job_id
                                )
                        else:
                            logger.warning(
                                "refill_failed_pre_insert",
                                sku_id=int(sku["id"]),
                                node_id=str(binding["node_id"]),
                                error=str(exc),
                                error_type=type(exc).__name__,
                            )
                        # NO raise — continue with next binding.

        return counters

    # === Private DB helpers ===

    def _list_active_skus(self, conn, *, limit: int) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                select * from skus
                where is_active = true
                order by id
                limit %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    def _get_sku_projection(self, conn, sku_id: int) -> dict[str, int]:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                  count(*) filter (where status = 'available')          as available,
                  count(*) filter (where status = 'pending_validation') as pending_validation
                from proxy_inventory
                where sku_id = %s
                """,
                (sku_id,),
            )
            inv_row = cur.fetchone() or {}
            cur.execute(
                """
                select coalesce(sum(count), 0) as queued_or_running
                from jobs
                where sku_id = %s and status in ('queued', 'running')
                """,
                (sku_id,),
            )
            jobs_row = cur.fetchone() or {}
        return {
            "available": int(inv_row.get("available") or 0),
            "pending_validation": int(inv_row.get("pending_validation") or 0),
            "queued_or_running": int(jobs_row.get("queued_or_running") or 0),
        }

    def _list_active_bindings_with_capacity(
        self, conn, *, sku_id: int, allow_degraded: bool
    ) -> list[dict[str, Any]]:
        with conn.cursor() as cur:
            cur.execute(
                """
                select
                  b.sku_id,
                  b.node_id,
                  b.weight                                                    as binding_weight,
                  least(b.max_batch_size, n.max_batch_size)                   as effective_max_batch,
                  n.max_parallel_jobs                                         as max_parallel_jobs,
                  n.runtime_status                                            as runtime_status
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

    def _count_in_flight_jobs(self, conn, *, node_id: str) -> int:
        with conn.cursor() as cur:
            cur.execute(
                """
                select count(*) as c
                from jobs
                where node_id = %s and status in ('queued', 'running')
                """,
                (node_id,),
            )
            row = cur.fetchone() or {}
        return int(row.get("c") or 0)

    def _insert_refill_job(
        self,
        conn,
        *,
        job_id: str,
        sku_id: int,
        node_id: str,
        count: int,
        priority: int,
        product: str,
        payload: dict[str, Any],
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into jobs (
                  id, status, count, product, node_id, sku_id,
                  reason, priority, payload, profile, available_at
                )
                values (
                  %s, 'queued', %s, %s, %s, %s,
                  'refill', %s, %s, %s, now()
                )
                """,
                (
                    job_id,
                    count,
                    product,
                    node_id,
                    sku_id,
                    priority,
                    Jsonb(payload),
                    Jsonb(PRODUCTION_PROFILE),
                ),
            )

    def _set_job_start_port(self, conn, *, job_id: str, start_port: int) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                update jobs
                set start_port = %s, updated_at = now()
                where id = %s
                """,
                (start_port, job_id),
            )

    def _build_refill_payload(self, *, sku: dict[str, Any], count: int) -> dict[str, Any]:
        return {
            "profile": PRODUCTION_PROFILE,
            "sku_code": sku["code"],
            "protocol": sku["protocol"],
            "geo_code": sku["geo_code"],
            "validation_require_ipv6": bool(sku["validation_require_ipv6"]),
            "count": count,
            "reason": "refill",
        }
