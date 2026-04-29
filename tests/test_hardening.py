"""Tests for B-7b.4 hardening: VALIDATION_STRICT_SSL + log_job_event."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest


def test_validation_strict_ssl_default_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """VALIDATION_STRICT_SSL defaults to true in config."""
    monkeypatch.delenv("VALIDATION_STRICT_SSL", raising=False)
    from orchestrator.config import get_config

    cfg = get_config()
    assert cfg.validation_strict_ssl is True


def test_validation_strict_ssl_false_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """VALIDATION_STRICT_SSL=false in env disables strict SSL verification."""
    monkeypatch.setenv("VALIDATION_STRICT_SSL", "false")
    from orchestrator.config import get_config

    cfg = get_config()
    assert cfg.validation_strict_ssl is False


def test_worker_log_job_event_on_request_error() -> None:
    """process_refill_job tags the failed event with error_type / error_class
    on httpx.RequestError, calls mark_failed, and does NOT raise."""
    job = {
        "id": "job-1",
        "sku_id": 7,
        "node_id": "node-x",
        "start_port": 30000,
        "count": 5,
    }

    fake_node = {
        "id": "node-x",
        "url": "http://10.0.0.5:8085",
        "api_key": None,
    }

    cursor = MagicMock()
    cursor.execute = MagicMock()
    cursor.fetchone = MagicMock(return_value=fake_node)
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=None)
    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)

    @contextmanager
    def fake_connect():
        yield conn

    captured: dict[str, Any] = {}

    def fake_mark_failed(job_id: str, error: str, event_data: dict[str, Any]) -> None:
        captured["job_id"] = job_id
        captured["error"] = error
        captured["event_data"] = event_data

    with (
        patch("orchestrator.worker.connect", new=fake_connect),
        patch("orchestrator.worker.generate", side_effect=httpx.RequestError("boom")),
        patch("orchestrator.worker.mark_failed", side_effect=fake_mark_failed),
    ):
        from orchestrator.worker import process_refill_job

        # MUST NOT raise — worker loop must continue.
        process_refill_job(job)

    assert captured["job_id"] == "job-1"
    assert captured["error"] == "node_unavailable"
    assert captured["event_data"]["error_type"] == "RequestError"
    assert captured["event_data"]["error_class"] == "request_error"
    assert captured["event_data"]["attempts"] == 1
    assert captured["event_data"]["sku_id"] == 7
    assert captured["event_data"]["node"] == "node-x"
