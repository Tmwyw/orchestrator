"""Watchdog: recovers stuck jobs, expired reservations, stale pending validation.

Phase 5 (B-8.2) folds pergb account lifecycle in here per design § 4.5:
mark expired traffic_accounts, cascade proxy_inventory state, archive
after grace, prune old samples.

Phase 5.4 / 5.5 (Wave D safety net) retry node-side block / unblock
calls that did not ack on the polling cycle: depleted accounts whose
node_blocked is FALSE need another post_disable; active accounts whose
node_blocked is TRUE need another post_enable. Throttled by
last_(un)block_attempt_at >= 5 minutes ago.
"""

from __future__ import annotations

from orchestrator import node_client
from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.logging_setup import get_logger
from orchestrator.node_client import NodeAgentError

logger = get_logger("netrun-orchestrator-watchdog")

_PERGB_GRACE_DAYS = 3
_PERGB_SAMPLE_RETENTION_DAYS = 30
_PERGB_BLOCK_RETRY_THROTTLE_MINUTES = 5
_PERGB_BLOCK_RETRY_BATCH_LIMIT = 100


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
            "pergb_block_retries_attempted": 0,
            "pergb_block_retries_succeeded": 0,
            "pergb_unblock_retries_attempted": 0,
            "pergb_unblock_retries_succeeded": 0,
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

        # === Phase 5.4: retry post_disable on depleted accounts whose node
        # didn't ack — "user paying for 1 GB but receiving unlimited" is the
        # failure mode this prevents.
        self._retry_pending_blocks(counters)

        # === Phase 5.5: mirror — retry post_enable after a top-up reactivation
        # whose enable RTT didn't ack. Without this the user pays for a top-up
        # but stays locked out at the nftables layer.
        self._retry_pending_unblocks(counters)

        if counters["pergb_block_retries_attempted"] or counters["pergb_unblock_retries_attempted"]:
            logger.info(
                "watchdog_pergb_safety_net",
                blocks_attempted=counters["pergb_block_retries_attempted"],
                blocks_succeeded=counters["pergb_block_retries_succeeded"],
                unblocks_attempted=counters["pergb_unblock_retries_attempted"],
                unblocks_succeeded=counters["pergb_unblock_retries_succeeded"],
            )

        return counters

    # === safety-net retries (Wave D) ===

    def _retry_pending_blocks(self, counters: dict[str, int]) -> None:
        """Find depleted accounts whose post_disable hasn't been acked and retry."""
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select t.id           as account_id,
                           i.port         as port,
                           n.url          as node_url,
                           n.api_key      as node_api_key,
                           i.node_id      as node_id
                    from traffic_accounts t
                    join proxy_inventory i on i.id = t.inventory_id
                    join nodes n on n.id = i.node_id
                    where t.status = 'depleted'
                      and t.node_blocked = false
                      and (t.last_block_attempt_at is null
                           or t.last_block_attempt_at < now() - (%s || ' minutes')::interval)
                    order by t.last_block_attempt_at nulls first
                    limit %s
                    """,
                    (_PERGB_BLOCK_RETRY_THROTTLE_MINUTES, _PERGB_BLOCK_RETRY_BATCH_LIMIT),
                )
                pending = list(cur.fetchall())

            for row in pending:
                counters["pergb_block_retries_attempted"] += 1
                ok = self._call_disable(
                    node_url=str(row["node_url"]),
                    node_api_key=(str(row["node_api_key"]) if row.get("node_api_key") else None),
                    port=int(row["port"]),
                    account_id=int(row["account_id"]),
                    node_id=str(row["node_id"]),
                )
                with conn.cursor() as cur:
                    if ok:
                        counters["pergb_block_retries_succeeded"] += 1
                        cur.execute(
                            """
                            update traffic_accounts
                            set node_blocked = true,
                                last_block_attempt_at = now(),
                                updated_at = now()
                            where id = %s
                            """,
                            (int(row["account_id"]),),
                        )
                    else:
                        cur.execute(
                            """
                            update traffic_accounts
                            set last_block_attempt_at = now(),
                                updated_at = now()
                            where id = %s
                            """,
                            (int(row["account_id"]),),
                        )

    def _retry_pending_unblocks(self, counters: dict[str, int]) -> None:
        """Find active accounts still flagged blocked and retry post_enable."""
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select t.id           as account_id,
                           i.port         as port,
                           n.url          as node_url,
                           n.api_key      as node_api_key,
                           i.node_id      as node_id
                    from traffic_accounts t
                    join proxy_inventory i on i.id = t.inventory_id
                    join nodes n on n.id = i.node_id
                    where t.status = 'active'
                      and t.node_blocked = true
                      and (t.last_unblock_attempt_at is null
                           or t.last_unblock_attempt_at < now() - (%s || ' minutes')::interval)
                    order by t.last_unblock_attempt_at nulls first
                    limit %s
                    """,
                    (_PERGB_BLOCK_RETRY_THROTTLE_MINUTES, _PERGB_BLOCK_RETRY_BATCH_LIMIT),
                )
                pending = list(cur.fetchall())

            for row in pending:
                counters["pergb_unblock_retries_attempted"] += 1
                ok = self._call_enable(
                    node_url=str(row["node_url"]),
                    node_api_key=(str(row["node_api_key"]) if row.get("node_api_key") else None),
                    port=int(row["port"]),
                    account_id=int(row["account_id"]),
                    node_id=str(row["node_id"]),
                )
                with conn.cursor() as cur:
                    if ok:
                        counters["pergb_unblock_retries_succeeded"] += 1
                        cur.execute(
                            """
                            update traffic_accounts
                            set node_blocked = false,
                                last_unblock_attempt_at = now(),
                                updated_at = now()
                            where id = %s
                            """,
                            (int(row["account_id"]),),
                        )
                    else:
                        cur.execute(
                            """
                            update traffic_accounts
                            set last_unblock_attempt_at = now(),
                                updated_at = now()
                            where id = %s
                            """,
                            (int(row["account_id"]),),
                        )

    def _call_disable(
        self,
        *,
        node_url: str,
        node_api_key: str | None,
        port: int,
        account_id: int,
        node_id: str,
    ) -> bool:
        try:
            node_client.post_disable(node_url, node_api_key, port)
        except NodeAgentError as exc:
            logger.warning(
                "watchdog_pergb_block_retry_failed",
                account_id=account_id,
                node_id=node_id,
                port=port,
                error=str(exc),
                status_code=exc.status_code,
            )
            return False
        return True

    def _call_enable(
        self,
        *,
        node_url: str,
        node_api_key: str | None,
        port: int,
        account_id: int,
        node_id: str,
    ) -> bool:
        try:
            node_client.post_enable(node_url, node_api_key, port)
        except NodeAgentError as exc:
            logger.warning(
                "watchdog_pergb_unblock_retry_failed",
                account_id=account_id,
                node_id=node_id,
                port=port,
                error=str(exc),
                status_code=exc.status_code,
            )
            return False
        return True
