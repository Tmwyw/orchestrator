"""Watchdog: recovers stuck jobs, expired reservations, stale pending validation."""

from __future__ import annotations

from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.logging_setup import get_logger

logger = get_logger("netrun-orchestrator-watchdog")


class WatchdogService:
    """Single-pass watchdog over jobs / orders / proxy_inventory / delivery_files."""

    def run_once(self) -> dict[str, int]:
        counters: dict[str, int] = {
            "jobs_failed_running": 0,
            "orders_released_expired": 0,
            "inventory_invalidated_stale": 0,
            "delivery_content_expired": 0,
        }
        cfg = get_config()

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update jobs
                set status = 'failed',
                    error = 'watchdog_running_timeout',
                    updated_at = now()
                where status = 'running'
                  and updated_at < now() - (%s || ' seconds')::interval
                returning id
                """,
                (cfg.watchdog_running_timeout_sec,),
            )
            counters["jobs_failed_running"] = len(cur.fetchall())

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id, order_ref, reservation_key
                    from orders
                    where status = 'reserved' and expires_at < now()
                    order by expires_at asc
                    limit 500
                    """
                )
                expired = list(cur.fetchall())
            for order in expired:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        update proxy_inventory
                        set status = 'available',
                            reservation_key = null,
                            reserved_at = null,
                            updated_at = now()
                        where reservation_key = %s and status = 'reserved'
                        """,
                        (order["reservation_key"],),
                    )
                    cur.execute(
                        """
                        update orders
                        set status = 'released',
                            released_at = now(),
                            updated_at = now()
                        where id = %s and status = 'reserved'
                        """,
                        (order["id"],),
                    )
                counters["orders_released_expired"] += 1
                logger.info("watchdog_order_released", order_ref=order["order_ref"])

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update proxy_inventory
                set status = 'invalid',
                    validation_error = 'watchdog_pending_validation_timeout',
                    validated_at = now(),
                    updated_at = now()
                where status = 'pending_validation'
                  and created_at < now() - (%s || ' seconds')::interval
                returning id
                """,
                (cfg.watchdog_pending_validation_timeout_sec,),
            )
            counters["inventory_invalidated_stale"] = len(cur.fetchall())

        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update delivery_files
                set content = null
                where content is not null
                  and content_expires_at < now()
                returning id
                """
            )
            counters["delivery_content_expired"] = len(cur.fetchall())

        return counters
