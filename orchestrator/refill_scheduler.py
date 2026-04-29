"""Refill scheduler: runs RefillService.run_once() in a loop."""

from __future__ import annotations

import time

from orchestrator.config import get_config
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.refill import RefillService

configure_logging()
logger = get_logger("netrun-orchestrator-refill-scheduler")


def run_loop() -> None:
    cfg = get_config()
    service = RefillService()
    interval = max(5, cfg.refill_interval_sec)
    logger.info("refill_scheduler_started", interval_sec=interval)
    while True:
        try:
            counters = service.run_once()
            logger.info("refill_cycle_completed", counters=counters)
        except Exception:
            logger.exception("refill_loop_error")
        time.sleep(interval)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
