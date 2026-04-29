"""Watchdog scheduler: runs WatchdogService.run_once() in a loop."""

from __future__ import annotations

import logging
import time

from orchestrator.config import get_config
from orchestrator.watchdog import WatchdogService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("netrun-orchestrator-watchdog-scheduler")


def run_loop() -> None:
    cfg = get_config()
    service = WatchdogService()
    interval = max(10, cfg.watchdog_interval_sec)
    logger.info("watchdog scheduler started interval=%ss", interval)
    while True:
        try:
            counters = service.run_once()
            if any(v > 0 for v in counters.values()):
                logger.info("watchdog summary: %s", counters)
        except Exception:
            logger.exception("watchdog loop error")
        time.sleep(interval)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
