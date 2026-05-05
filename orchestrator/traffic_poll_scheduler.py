"""Traffic-poll scheduler: runs ``TrafficPollService.run_once()`` in a loop.

Standalone systemd unit ``netrun-orchestrator-traffic-poll.service`` (the
6th, alongside the five from B-7a). Mirrors ``watchdog_scheduler.py`` shape:
sync ``time.sleep(interval)`` loop, prometheus duration/total per cycle,
structured logs.

The pre-tick gauge refresh hits Postgres twice (active + depleted counts +
oldest active.last_polled_at) — cheap because the indexes are partial.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.metrics import (
    SCHEDULER_RUN_DURATION_SEC,
    SCHEDULER_RUN_TOTAL,
    TRAFFIC_ACCOUNTS_ACTIVE,
    TRAFFIC_ACCOUNTS_DEPLETED,
    TRAFFIC_POLL_LAG_SEC,
)
from orchestrator.traffic_poll import TrafficPollService

configure_logging()
logger = get_logger("netrun-orchestrator-traffic-poll-scheduler")

_SCHED = "traffic_poll"


def _refresh_gauges() -> None:
    """One small SELECT block — keeps the gauges fresh per cycle."""
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                select
                  count(*) filter (where status = 'active')   as active,
                  count(*) filter (where status = 'depleted') as depleted,
                  min(last_polled_at) filter (where status = 'active') as oldest_active_polled_at
                from traffic_accounts
                """
            )
            row = cur.fetchone() or {}
        TRAFFIC_ACCOUNTS_ACTIVE.set(int(row.get("active") or 0))
        TRAFFIC_ACCOUNTS_DEPLETED.set(int(row.get("depleted") or 0))
        oldest = row.get("oldest_active_polled_at")
        if oldest is None:
            TRAFFIC_POLL_LAG_SEC.set(0)
        else:
            now = datetime.now(timezone.utc)
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            TRAFFIC_POLL_LAG_SEC.set(max(0.0, (now - oldest).total_seconds()))
    except Exception:
        # Gauge refresh is best-effort; do not crash the scheduler over it.
        logger.exception("traffic_poll_gauge_refresh_failed")


def run_loop() -> None:
    cfg = get_config()
    service = TrafficPollService()
    interval = max(cfg.traffic_poll_min_interval_sec, cfg.traffic_poll_interval_sec)
    logger.info(
        "traffic_poll_scheduler_started",
        interval_sec=interval,
        min_interval_sec=cfg.traffic_poll_min_interval_sec,
        request_timeout_sec=cfg.traffic_poll_request_timeout_sec,
        degrade_after=cfg.traffic_poll_degrade_after,
    )
    while True:
        with SCHEDULER_RUN_DURATION_SEC.labels(scheduler=_SCHED).time():
            try:
                _refresh_gauges()
                counters = service.run_once()
                SCHEDULER_RUN_TOTAL.labels(scheduler=_SCHED, status="success").inc()
                if counters.skipped_overlap or any(
                    v
                    for k, v in counters.as_dict().items()
                    if k != "skipped_overlap" and isinstance(v, int) and v > 0
                ):
                    logger.info("traffic_poll_cycle_completed", **counters.as_dict())
            except Exception:
                SCHEDULER_RUN_TOTAL.labels(scheduler=_SCHED, status="failed").inc()
                logger.exception("traffic_poll_loop_error")
        time.sleep(interval)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
