"""Watchdog scheduler: runs WatchdogService.run_once() in a loop."""

from __future__ import annotations

import time

from orchestrator.config import get_config
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.watchdog import WatchdogService

configure_logging()
logger = get_logger("netrun-orchestrator-watchdog-scheduler")


def run_loop() -> None:
    cfg = get_config()
    service = WatchdogService()
    interval = max(10, cfg.watchdog_interval_sec)
    logger.info("watchdog_scheduler_started", interval_sec=interval)
    while True:
        try:
            counters = service.run_once()
            if any(v > 0 for v in counters.values()):
                logger.info("watchdog_cycle_completed", counters=counters)
        except Exception:
            logger.exception("watchdog_loop_error")
        time.sleep(interval)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
