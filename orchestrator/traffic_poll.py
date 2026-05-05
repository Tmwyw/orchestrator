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

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _AccountRow:
    account_id: int
    inventory_id: int
    bytes_quota: int
    bytes_used: int
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

    def run_once(self) -> PollCounters:
        if not self._lock.acquire(blocking=False):
            logger.warning("traffic_poll_skipped_overlap")
            return PollCounters(skipped_overlap=True)
        try:
            return self._poll_cycle()
        finally:
            self._lock.release()

    # === core loop ===

    def _poll_cycle(self) -> PollCounters:
        counters = PollCounters()
        cfg = get_config()
        rows = self._fetch_active_accounts()
        if not rows:
            return counters

        by_node: dict[str, list[_AccountRow]] = {}
        for row in rows:
            by_node.setdefault(row.node_id, []).append(row)

        for node_id, accounts in by_node.items():
            self._poll_one_node(
                node_id=node_id,
                accounts=accounts,
                counters=counters,
                degrade_after=cfg.traffic_poll_degrade_after,
                request_timeout_sec=cfg.traffic_poll_request_timeout_sec,
            )
        return counters

    def _poll_one_node(
        self,
        *,
        node_id: str,
        accounts: list[_AccountRow],
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
        account: _AccountRow,
        sample: dict[str, int],
        counters: PollCounters,
    ) -> None:
        bytes_in_total = int(sample.get("bytes_in", 0)) + int(sample.get("bytes_in6", 0))
        bytes_out_total = int(sample.get("bytes_out", 0)) + int(sample.get("bytes_out6", 0))

        last_in = account.last_polled_bytes_in
        last_out = account.last_polled_bytes_out
        reset_detected = False
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
                reset_detected = True

        new_bytes_used = account.bytes_used + delta_in + delta_out
        flipped_to_depleted = self._persist_sample(
            account=account,
            bytes_in_total=bytes_in_total,
            bytes_out_total=bytes_out_total,
            delta_in=delta_in,
            delta_out=delta_out,
            reset_detected=reset_detected,
            new_bytes_used=new_bytes_used,
        )
        counters.accounts_polled += 1

        if account.sku_code:
            if delta_in:
                TRAFFIC_BYTES_TOTAL.labels(sku_code=account.sku_code, direction="in").inc(delta_in)
            if delta_out:
                TRAFFIC_BYTES_TOTAL.labels(sku_code=account.sku_code, direction="out").inc(delta_out)

        if flipped_to_depleted:
            counters.accounts_depleted += 1
            if new_bytes_used > account.bytes_quota:
                TRAFFIC_OVER_USAGE_TOTAL.inc()
            self._fire_disable(account, counters)

    def _persist_sample(
        self,
        *,
        account: _AccountRow,
        bytes_in_total: int,
        bytes_out_total: int,
        delta_in: int,
        delta_out: int,
        reset_detected: bool,
        new_bytes_used: int,
    ) -> bool:
        """Returns True iff this sample flipped status active → depleted."""
        flipped = False
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into traffic_samples (
                  account_id, bytes_in, bytes_out,
                  bytes_in_delta, bytes_out_delta, counter_reset_detected
                )
                values (%s, %s, %s, %s, %s, %s)
                """,
                (
                    account.account_id,
                    bytes_in_total,
                    bytes_out_total,
                    delta_in,
                    delta_out,
                    reset_detected,
                ),
            )
            cur.execute(
                """
                update traffic_accounts
                set bytes_used = %s,
                    last_polled_bytes_in = %s,
                    last_polled_bytes_out = %s,
                    last_polled_at = now(),
                    updated_at = now()
                where id = %s
                """,
                (
                    new_bytes_used,
                    bytes_in_total,
                    bytes_out_total,
                    account.account_id,
                ),
            )
            if new_bytes_used >= account.bytes_quota:
                cur.execute(
                    """
                    update traffic_accounts
                    set status = 'depleted',
                        depleted_at = now(),
                        updated_at = now()
                    where id = %s and status = 'active'
                    returning id
                    """,
                    (account.account_id,),
                )
                flipped = bool(cur.fetchone())
        return flipped

    def _fire_disable(self, account: _AccountRow, counters: PollCounters) -> None:
        """Best-effort post_disable on the node-agent. Failure is logged but
        does not back-out the depletion DB write — next cycle will retry."""
        try:
            node_client.post_disable(
                account.node_url,
                account.node_api_key,
                account.port,
            )
            counters.accounts_disabled += 1
            logger.info(
                "traffic_account_depleted",
                account_id=account.account_id,
                node_id=account.node_id,
                port=account.port,
            )
        except NodeAgentError as exc:
            logger.warning(
                "traffic_account_disable_failed",
                account_id=account.account_id,
                node_id=account.node_id,
                port=account.port,
                error=str(exc),
                status_code=exc.status_code,
            )

    # === DB helpers ===

    def _fetch_active_accounts(self) -> list[_AccountRow]:
        rows: list[_AccountRow] = []
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select t.id            as account_id,
                       t.inventory_id  as inventory_id,
                       t.bytes_quota   as bytes_quota,
                       t.bytes_used    as bytes_used,
                       t.last_polled_bytes_in,
                       t.last_polled_bytes_out,
                       i.node_id       as node_id,
                       i.port          as port,
                       n.url           as node_url,
                       n.api_key       as node_api_key,
                       s.code          as sku_code
                from traffic_accounts t
                join proxy_inventory i on i.id = t.inventory_id
                join nodes n on n.id = i.node_id
                join skus s on s.id = i.sku_id
                where t.status = 'active'
                """
            )
            for r in cur.fetchall():
                rows.append(
                    _AccountRow(
                        account_id=int(r["account_id"]),
                        inventory_id=int(r["inventory_id"]),
                        bytes_quota=int(r["bytes_quota"]),
                        bytes_used=int(r["bytes_used"]),
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
