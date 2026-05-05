"""Watchdog: recovers stuck jobs, expired reservations, stale pending validation.

Phase 5 (B-8.2) folds pergb account lifecycle in here per design § 4.5:
mark expired traffic_accounts, cascade proxy_inventory state, archive
after grace, prune old samples.
"""

from __future__ import annotations

from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.logging_setup import get_logger

logger = get_logger("netrun-orchestrator-watchdog")

_PERGB_GRACE_DAYS = 3
_PERGB_SAMPLE_RETENTION_DAYS = 30


class WatchdogService:
    """Single-pass watchdog over jobs / orders / proxy_inventory / delivery_files
    / traffic_accounts (pergb)."""

    def run_once(self) -> dict[str, int]:
        counters: dict[str, int] = {
            "jobs_failed_running": 0,
            "orders_released_expired": 0,
            "inventory_invalidated_stale": 0,
            "delivery_content_expired": 0,
            "pergb_accounts_expired": 0,
            "pergb_accounts_archived": 0,
            "pergb_samples_pruned": 0,
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

        # === Phase 5: pergb cleanup (Wave B-8.2 § 4.5) ===

        # 5.1 Mark active/depleted accounts whose lease has elapsed → 'expired',
        # and cascade proxy_inventory.allocated_pergb → 'expired_grace' so the
        # inventory becomes visible to the existing per-piece grace window.
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update traffic_accounts
                set status = 'expired', updated_at = now()
                where status in ('active', 'depleted')
                  and now() >= expires_at
                returning id, inventory_id
                """
            )
            expired_rows = list(cur.fetchall())
            counters["pergb_accounts_expired"] = len(expired_rows)
            if expired_rows:
                inventory_ids = [int(r["inventory_id"]) for r in expired_rows]
                cur.execute(
                    """
                    update proxy_inventory
                    set status = 'expired_grace', updated_at = now()
                    where id = any(%s) and status = 'allocated_pergb'
                    """,
                    (inventory_ids,),
                )

        # 5.2 Archive accounts past the 3-day grace; cascade inventory to archived.
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update traffic_accounts
                set status = 'archived', updated_at = now()
                where status = 'expired'
                  and expires_at < now() - (%s || ' days')::interval
                returning id, inventory_id
                """,
                (_PERGB_GRACE_DAYS,),
            )
            archived_rows = list(cur.fetchall())
            counters["pergb_accounts_archived"] = len(archived_rows)
            if archived_rows:
                inventory_ids = [int(r["inventory_id"]) for r in archived_rows]
                cur.execute(
                    """
                    update proxy_inventory
                    set status = 'archived', archived_at = now(), updated_at = now()
                    where id = any(%s) and status = 'expired_grace'
                    """,
                    (inventory_ids,),
                )

        # 5.3 Prune old traffic_samples (retention).
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                delete from traffic_samples
                where collected_at < now() - (%s || ' days')::interval
                returning id
                """,
                (_PERGB_SAMPLE_RETENTION_DAYS,),
            )
            counters["pergb_samples_pruned"] = len(cur.fetchall())

        if any(
            counters[k] for k in ("pergb_accounts_expired", "pergb_accounts_archived", "pergb_samples_pruned")
        ):
            logger.info(
                "watchdog_pergb_cleanup",
                expired=counters["pergb_accounts_expired"],
                archived=counters["pergb_accounts_archived"],
                samples_pruned=counters["pergb_samples_pruned"],
            )

        return counters
