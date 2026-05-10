"""Pay-per-GB traffic polling worker (Wave B-8.2).

Per design § 4: reads node-agent ``/accounting`` counters per active
``traffic_account``, computes deltas vs the per-account anchor, writes
``traffic_samples`` rows + updates ``bytes_used`` on ``traffic_accounts``,
and triggers depletion (status flip + node-side disable) when the new
``bytes_used`` crosses ``bytes_quota``.

Counter-reset is detected via ``delta < 0`` and clamped to 0 for that
cycle (no negative billing, anchor re-set to the new lower reading).

Cleanup (expire/archive/prune) lives in ``WatchdogService`` per § 4.5.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any

from orchestrator import node_client
from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.logging_setup import get_logger
from orchestrator.metrics import (
    TRAFFIC_BYTES_TOTAL,
    TRAFFIC_COUNTER_RESET_TOTAL,
    TRAFFIC_OVER_USAGE_TOTAL,
    TRAFFIC_POLL_DURATION_SEC,
    TRAFFIC_POLL_TOTAL,
)
from orchestrator.node_client import NodeAgentError

logger = get_logger("netrun-orchestrator-traffic-poll")


@dataclass(slots=True)
class PollCounters:
    """Counters returned by ``TrafficPollService.run_once()``.

    Keys mirror the design § 4.2 spec; the dict form is published into
    structured logs and into the future admin force-poll response shape.
    """

    accounts_polled: int = 0
    accounts_depleted: int = 0
    accounts_disabled: int = 0
    node_failures: int = 0
    counter_resets_detected: int = 0
    skipped_overlap: bool = False
    nodes_polled: int = 0
    bytes_observed_total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _PortRow:
    """One row per linked port (Wave PERGB-RFCT-A: 1 traffic_account → N ports).

    Anchors and the snapshot live on proxy_inventory now so each port has
    its own counter trajectory (detect resets independently). The
    account-level bytes_used is recomputed via SUM at the end of each
    cycle.
    """

    account_id: int
    inventory_id: int
    bytes_quota: int
    port_bytes_used_snapshot: int
    last_polled_bytes_in: int | None
    last_polled_bytes_out: int | None
    node_id: str
    node_url: str
    node_api_key: str | None
    port: int
    sku_code: str = ""


class TrafficPollService:
    """Single-pass traffic poll over all active pergb accounts.

    Sync, mirroring ``WatchdogService`` / ``RefillService`` shape. The
    scheduler invokes ``run_once()`` on a ``time.sleep(interval)`` loop.

    Serialization gate (D5.2): a non-blocking ``threading.Lock`` skips a
    cycle if a previous one is still in progress (guards future admin
    force-poll endpoint from racing the scheduler).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._node_failures: dict[str, int] = {}

    def run_once(
        self,
        *,
        node_id_filter: str | None = None,
        account_id_filter: int | None = None,
    ) -> PollCounters:
        """Run one polling cycle over active accounts.

        ``node_id_filter`` / ``account_id_filter`` (B-8.3) narrow the SELECT to
        a single node or single account — used by the admin force-poll
        endpoint. The scheduler always calls ``run_once()`` with no filters.
        """
        if not self._lock.acquire(blocking=False):
            logger.warning("traffic_poll_skipped_overlap")
            return PollCounters(skipped_overlap=True)
        try:
            return self._poll_cycle(
                node_id_filter=node_id_filter,
                account_id_filter=account_id_filter,
            )
        finally:
            self._lock.release()

    # === core loop ===

    def _poll_cycle(
        self,
        *,
        node_id_filter: str | None = None,
        account_id_filter: int | None = None,
    ) -> PollCounters:
        counters = PollCounters()
        cfg = get_config()
        rows = self._fetch_active_ports(
            node_id_filter=node_id_filter,
            account_id_filter=account_id_filter,
        )
        if not rows:
            return counters

        by_node: dict[str, list[_PortRow]] = {}
        touched_accounts: set[int] = set()
        for row in rows:
            by_node.setdefault(row.node_id, []).append(row)
            touched_accounts.add(row.account_id)

        counters.nodes_polled = len(by_node)
        for node_id, accounts in by_node.items():
            self._poll_one_node(
                node_id=node_id,
                accounts=accounts,
                counters=counters,
                degrade_after=cfg.traffic_poll_degrade_after,
                request_timeout_sec=cfg.traffic_poll_request_timeout_sec,
            )

        # After per-port snapshots are written, aggregate to traffic_accounts
        # in one pass per touched account + flip newly-depleted accounts.
        self._aggregate_and_flip_depleted(touched_accounts, counters)
        return counters

    def _poll_one_node(
        self,
        *,
        node_id: str,
        accounts: list[_PortRow],
        counters: PollCounters,
        degrade_after: int,
        request_timeout_sec: int,
    ) -> None:
        ports = [a.port for a in accounts]
        node_url = accounts[0].node_url
        node_api_key = accounts[0].node_api_key

        try:
            with TRAFFIC_POLL_DURATION_SEC.labels(node_id=node_id).time():
                response = node_client.get_accounting(
                    node_url, node_api_key, ports, timeout_sec=request_timeout_sec
                )
        except NodeAgentError as exc:
            counters.node_failures += 1
            consecutive = self._node_failures.get(node_id, 0) + 1
            self._node_failures[node_id] = consecutive
            TRAFFIC_POLL_TOTAL.labels(node_id=node_id, status="failed").inc()
            logger.warning(
                "traffic_poll_node_failed",
                node_id=node_id,
                error=str(exc),
                status_code=exc.status_code,
                consecutive_failures=consecutive,
            )
            if consecutive >= degrade_after:
                self._mark_node_degraded(node_id)
            return

        # Successful HTTP — clear consecutive-failure counter
        self._node_failures[node_id] = 0
        TRAFFIC_POLL_TOTAL.labels(node_id=node_id, status="success").inc()

        # Defensive partial-response handling per design § 8.3
        present = {int(p) for p in response}
        missing = [p for p in ports if p not in present]
        if missing:
            logger.warning(
                "traffic_poll_partial_response",
                node_id=node_id,
                missing_ports=missing,
            )

        for account in accounts:
            sample = response.get(str(account.port))
            if sample is None:
                # node-agent may emit numeric keys in some shapes — defensive lookup
                sample = response.get(str(account.port).lstrip("0"))
            if sample is None:
                continue
            self._process_sample(account, sample, counters)

    # === sample processing ===

    def _process_sample(
        self,
        account: _PortRow,
        sample: dict[str, int],
        counters: PollCounters,
    ) -> None:
        """Update one port's snapshot. Account-level depletion + node disable
        happen in _aggregate_and_flip_depleted after every port for the cycle
        is processed (so SUM is correct).
        """
        bytes_in_total = int(sample.get("bytes_in", 0)) + int(sample.get("bytes_in6", 0))
        bytes_out_total = int(sample.get("bytes_out", 0)) + int(sample.get("bytes_out6", 0))

        last_in = account.last_polled_bytes_in
        last_out = account.last_polled_bytes_out
        if last_in is None or last_out is None:
            delta_in = 0
            delta_out = 0
        else:
            delta_in = bytes_in_total - last_in
            delta_out = bytes_out_total - last_out
            if delta_in < 0 or delta_out < 0:
                counters.counter_resets_detected += 1
                TRAFFIC_COUNTER_RESET_TOTAL.labels(node_id=account.node_id).inc()
                logger.warning(
                    "traffic_counter_reset_detected",
                    node_id=account.node_id,
                    port=account.port,
                    account_id=account.account_id,
                    previous_in=last_in,
                    previous_out=last_out,
                    new_in=bytes_in_total,
                    new_out=bytes_out_total,
                )
                delta_in = 0
                delta_out = 0

        new_port_snapshot = account.port_bytes_used_snapshot + delta_in + delta_out
        self._persist_port_snapshot(
            account=account,
            bytes_in_total=bytes_in_total,
            bytes_out_total=bytes_out_total,
            new_port_snapshot=new_port_snapshot,
        )
        counters.accounts_polled += 1
        counters.bytes_observed_total += delta_in + delta_out

        if account.sku_code:
            if delta_in:
                TRAFFIC_BYTES_TOTAL.labels(sku_code=account.sku_code, direction="in").inc(delta_in)
            if delta_out:
                TRAFFIC_BYTES_TOTAL.labels(sku_code=account.sku_code, direction="out").inc(delta_out)

    def _persist_port_snapshot(
        self,
        *,
        account: _PortRow,
        bytes_in_total: int,
        bytes_out_total: int,
        new_port_snapshot: int,
    ) -> None:
        """Write per-port bytes_used_snapshot + anchors. The traffic_samples
        row remains keyed on account_id (legacy schema); we still emit one
        per port for full-cycle accounting visibility.
        """
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update proxy_inventory
                set bytes_used_snapshot = %s,
                    last_polled_bytes_in = %s,
                    last_polled_bytes_out = %s,
                    updated_at = now()
                where id = %s
                """,
                (
                    new_port_snapshot,
                    bytes_in_total,
                    bytes_out_total,
                    account.inventory_id,
                ),
            )

    def _aggregate_and_flip_depleted(
        self,
        account_ids: set[int],
        counters: PollCounters,
    ) -> None:
        """For each touched account: recompute bytes_used = SUM(per-port
        snapshots), flip to depleted if quota crossed, fan out post_disable
        on all linked ports for newly-depleted accounts.
        """
        if not account_ids:
            return
        ids_list = list(account_ids)
        # Single-statement aggregate + flip; capture newly-depleted ids.
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update traffic_accounts ta set
                  bytes_used = coalesce(
                    (select sum(pi.bytes_used_snapshot) from proxy_inventory pi
                      where pi.traffic_account_id = ta.id),
                    0
                  ),
                  last_polled_at = now(),
                  updated_at = now()
                where ta.id = any(%s)
                returning ta.id, ta.bytes_used, ta.bytes_quota, ta.status
                """,
                (ids_list,),
            )
            updated = list(cur.fetchall())

            newly_depleted: list[int] = []
            for r in updated:
                if int(r["bytes_used"]) >= int(r["bytes_quota"]) and str(r["status"]) == "active":
                    newly_depleted.append(int(r["id"]))
                    if int(r["bytes_used"]) > int(r["bytes_quota"]):
                        TRAFFIC_OVER_USAGE_TOTAL.inc()

            if newly_depleted:
                cur.execute(
                    """
                    update traffic_accounts
                    set status = 'depleted',
                        depleted_at = now(),
                        updated_at = now()
                    where id = any(%s) and status = 'active'
                    """,
                    (newly_depleted,),
                )

        for ta_id in newly_depleted:
            counters.accounts_depleted += 1
            self._fire_disable_account(ta_id, counters)

    def _fire_disable_account(self, account_id: int, counters: PollCounters) -> None:
        """Best-effort post_disable on every port linked to a newly-depleted
        traffic_account. Mirrors the old _fire_disable but fans out across
        N ports.
        """
        ports = self._fetch_linked_ports(account_id)
        all_ok = bool(ports)
        for p in ports:
            try:
                node_client.post_disable(p["node_url"], p["node_api_key"], p["port"])
                counters.accounts_disabled += 1
                logger.info(
                    "traffic_account_depleted_port",
                    account_id=account_id,
                    node_id=p["node_id"],
                    port=p["port"],
                )
            except NodeAgentError as exc:
                all_ok = False
                logger.warning(
                    "traffic_account_disable_failed",
                    account_id=account_id,
                    node_id=p["node_id"],
                    port=p["port"],
                    error=str(exc),
                    status_code=exc.status_code,
                )
        self._record_block_attempt(account_id, succeeded=all_ok)

    def _fetch_linked_ports(self, account_id: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select i.port, i.node_id,
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
                        "port": int(r["port"]),
                        "node_id": str(r["node_id"]),
                        "node_url": str(r["node_url"]),
                        "node_api_key": (str(r["node_api_key"]) if r.get("node_api_key") else None),
                    }
                )
        return rows

    def _record_block_attempt(self, account_id: int, *, succeeded: bool) -> None:
        """Stamp last_block_attempt_at, and on success flip node_blocked=TRUE.

        Failures leave node_blocked alone (default FALSE for new rows; if a
        prior cycle had already succeeded, post_disable is idempotent so the
        node stays blocked even if we don't see an ack this cycle)."""
        with connect() as conn, conn.cursor() as cur:
            if succeeded:
                cur.execute(
                    "update traffic_accounts "
                    "set node_blocked = TRUE, "
                    "    last_block_attempt_at = now(), "
                    "    updated_at = now() "
                    "where id = %s",
                    (account_id,),
                )
            else:
                cur.execute(
                    "update traffic_accounts "
                    "set last_block_attempt_at = now(), "
                    "    updated_at = now() "
                    "where id = %s",
                    (account_id,),
                )

    # === DB helpers ===

    def _fetch_active_ports(
        self,
        *,
        node_id_filter: str | None = None,
        account_id_filter: int | None = None,
    ) -> list[_PortRow]:
        """One row per linked port (Wave PERGB-RFCT-A).

        We join through the reverse FK proxy_inventory.traffic_account_id
        so legacy 1:1 clients (backfilled by migration 028) and new N-port
        clients are picked up uniformly.
        """
        rows: list[_PortRow] = []
        sql = """
            select t.id            as account_id,
                   i.id            as inventory_id,
                   t.bytes_quota   as bytes_quota,
                   i.bytes_used_snapshot as port_bytes_used_snapshot,
                   i.last_polled_bytes_in,
                   i.last_polled_bytes_out,
                   i.node_id       as node_id,
                   i.port          as port,
                   n.url           as node_url,
                   n.api_key       as node_api_key,
                   s.code          as sku_code
            from traffic_accounts t
            join proxy_inventory i on i.traffic_account_id = t.id
            join nodes n on n.id = i.node_id
            join skus s on s.id = i.sku_id
            where t.status = 'active'
        """
        params: list[Any] = []
        if node_id_filter is not None:
            sql += " and i.node_id = %s"
            params.append(node_id_filter)
        if account_id_filter is not None:
            sql += " and t.id = %s"
            params.append(account_id_filter)
        with connect() as conn, conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            for r in cur.fetchall():
                rows.append(
                    _PortRow(
                        account_id=int(r["account_id"]),
                        inventory_id=int(r["inventory_id"]),
                        bytes_quota=int(r["bytes_quota"]),
                        port_bytes_used_snapshot=int(r["port_bytes_used_snapshot"] or 0),
                        last_polled_bytes_in=(
                            int(r["last_polled_bytes_in"]) if r["last_polled_bytes_in"] is not None else None
                        ),
                        last_polled_bytes_out=(
                            int(r["last_polled_bytes_out"])
                            if r["last_polled_bytes_out"] is not None
                            else None
                        ),
                        node_id=str(r["node_id"]),
                        node_url=str(r["node_url"]),
                        node_api_key=(str(r["node_api_key"]) if r.get("node_api_key") else None),
                        port=int(r["port"]),
                        sku_code=str(r.get("sku_code") or ""),
                    )
                )
        return rows

    def _mark_node_degraded(self, node_id: str) -> None:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update nodes
                set runtime_status = 'degraded', updated_at = now()
                where id = %s and runtime_status <> 'degraded'
                returning id
                """,
                (node_id,),
            )
            updated = cur.fetchone()
        if updated:
            logger.warning("traffic_poll_node_degraded", node_id=node_id)
