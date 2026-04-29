"""Smoke tests for structlog bridge configuration."""

from __future__ import annotations

import io
import json
import logging
from contextlib import redirect_stderr


def test_configure_logging_emits_json() -> None:
    """After configure_logging, stdlib logger.info(...) renders as JSON to stderr."""
    from orchestrator.logging_setup import configure_logging

    buf = io.StringIO()
    with redirect_stderr(buf):
        configure_logging()
        logger = logging.getLogger("test_bridge")
        logger.info("test_event_with_arg key=%s", "value")

    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "test_event_with_arg key=value"
    assert payload["level"] == "info"
    assert payload["logger"] == "test_bridge"
    assert "timestamp" in payload


def test_get_logger_returns_bound_logger_with_kv_api() -> None:
    """get_logger() returns BoundLogger that accepts kwargs as structured fields."""
    from orchestrator.logging_setup import configure_logging, get_logger

    buf = io.StringIO()
    with redirect_stderr(buf):
        configure_logging()
        logger = get_logger("test_bound")
        logger.info("event_name", order_ref="ord_abc", count=42)

    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "event_name"
    assert payload["order_ref"] == "ord_abc"
    assert payload["count"] == 42
