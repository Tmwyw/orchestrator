"""Validation scheduler: runs ProxyValidationWorker.run_loop() in asyncio."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from orchestrator.config import get_config
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.validation_worker import ProxyValidationWorker

configure_logging()
logger = get_logger("netrun-orchestrator-validation-scheduler")


async def _run() -> None:
    cfg = get_config()
    worker = ProxyValidationWorker(
        batch_size=cfg.validation_batch_size,
        concurrency=cfg.validation_concurrency,
        poll_interval_sec=cfg.validation_poll_interval_sec,
    )
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("validation_scheduler_stop_signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Windows: asyncio doesn't support signal handlers in the default loop.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    await worker.run_loop(stop_event)


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
