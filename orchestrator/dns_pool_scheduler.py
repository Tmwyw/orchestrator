"""DNS pool scheduler: runs ``run_dns_pool_refresh()`` once every 24h.

Standalone systemd unit ``netrun-orchestrator-dns-pool.service`` (the 7th).
Uses the same shape as the other schedulers: sync ``time.sleep(interval)``
loop, prometheus duration/total per cycle, structured logs.

A randomised initial offset (0..3600s) prevents collision with the traffic
poll / refill schedulers and spreads load on the upstream feed across
deployments.
"""

from __future__ import annotations

import os
import time

from orchestrator.dns_pool import jittered_initial_delay_sec, run_dns_pool_refresh
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.metrics import SCHEDULER_RUN_DURATION_SEC, SCHEDULER_RUN_TOTAL

configure_logging()
logger = get_logger("netrun-orchestrator-dns-pool-scheduler")

_SCHED = "dns_pool"
DEFAULT_INTERVAL_SEC = 86400  # 24h


def _interval_sec() -> int:
    raw = os.getenv("DNS_POOL_REFRESH_INTERVAL_SEC")
    if not raw:
        return DEFAULT_INTERVAL_SEC
    try:
        value = int(raw)
        return max(60, value)
    except ValueError:
        return DEFAULT_INTERVAL_SEC


def run_loop() -> None:
    interval = _interval_sec()
    initial_delay = jittered_initial_delay_sec()
    logger.info(
        "dns_pool_scheduler_started",
        interval_sec=interval,
        initial_delay_sec=initial_delay,
    )
    time.sleep(initial_delay)
    while True:
        with SCHEDULER_RUN_DURATION_SEC.labels(scheduler=_SCHED).time():
            try:
                counters = run_dns_pool_refresh()
                SCHEDULER_RUN_TOTAL.labels(scheduler=_SCHED, status="success").inc()
                logger.info("dns_pool_cycle_completed", **counters)
            except Exception:
                SCHEDULER_RUN_TOTAL.labels(scheduler=_SCHED, status="failed").inc()
                logger.exception("dns_pool_loop_error")
        time.sleep(interval)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
