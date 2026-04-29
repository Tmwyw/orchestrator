"""Refill scheduler: runs RefillService.run_once() in a loop."""

from __future__ import annotations

import logging
import time

from orchestrator.config import get_config
from orchestrator.logging_setup import configure_logging
from orchestrator.refill import RefillService

configure_logging()
logger = logging.getLogger("netrun-orchestrator-refill-scheduler")


def run_loop() -> None:
    cfg = get_config()
    service = RefillService()
    interval = max(5, cfg.refill_interval_sec)
    logger.info("refill scheduler started interval=%ss", interval)
    while True:
        try:
            counters = service.run_once()
            logger.info("refill summary: %s", counters)
        except Exception:
            logger.exception("refill loop error")
        time.sleep(interval)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
