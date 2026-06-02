"""Egress watchdog scheduler: runs EgressWatchdogService.run_once() in a loop.

Wave WATCHDOG-EGRESS-CHECK — mirror of watchdog_scheduler.py. Separate
systemd service so the outbound-probe cadence is independent of the main
watchdog.
"""

from __future__ import annotations

import time

from orchestrator.config import get_config
from orchestrator.egress_watchdog import EgressWatchdogService
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.metrics import (
    EGRESS_WATCHDOG_ACTIONS,
    SCHEDULER_RUN_DURATION_SEC,
    SCHEDULER_RUN_TOTAL,
)

configure_logging()
logger = get_logger("netrun-orchestrator-egress-watchdog-scheduler")

_SCHED = "egress_watchdog"


def run_loop() -> None:
    cfg = get_config()
    service = EgressWatchdogService()
    interval = max(30, cfg.egress_watchdog_interval_sec)
    logger.info("egress_watchdog_scheduler_started", interval_sec=interval)
    while True:
        with SCHEDULER_RUN_DURATION_SEC.labels(scheduler=_SCHED).time():
            try:
                counters = service.run_once()
                SCHEDULER_RUN_TOTAL.labels(scheduler=_SCHED, status="success").inc()
                for action, n in counters.items():
                    if n:
                        EGRESS_WATCHDOG_ACTIONS.labels(action=action).inc(n)
                if any(v > 0 for v in counters.values()):
                    logger.info("egress_watchdog_cycle_completed", counters=counters)
            except Exception:
                SCHEDULER_RUN_TOTAL.labels(scheduler=_SCHED, status="failed").inc()
                logger.exception("egress_watchdog_loop_error")
        time.sleep(interval)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
