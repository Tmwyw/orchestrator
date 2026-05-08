"""Phase 5 pergb cleanup tests for WatchdogService (B-8.2 § 4.5)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _make_cursor(*, fetchall_queue: list[list[dict[str, Any]]]) -> MagicMock:
    cursor = MagicMock(name="cursor")
    cursor.execute = MagicMock()
    fa = list(fetchall_queue)
    cursor.fetchall = MagicMock(side_effect=lambda: fa.pop(0) if fa else [])
    cursor.fetchone = MagicMock(return_value=None)
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=None)
    return cursor


def _make_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.cursor = MagicMock(return_value=cursor)
    return conn


def _make_phased_connect(*phases: list[dict[str, Any]]):
    cursors = [_make_cursor(fetchall_queue=list(p)) for p in phases]
    conns = [_make_conn(c) for c in cursors]
    iterator = iter(conns)

    @contextmanager
    def fake_connect():
        yield next(iterator)

    return fake_connect, cursors


@pytest.fixture(autouse=True)
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_RUNNING_TIMEOUT_SEC", "1800")
    monkeypatch.setenv("WATCHDOG_PENDING_VALIDATION_TIMEOUT_SEC", "600")


def test_phase5_marks_expired_accounts_and_cascades_inventory() -> None:
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],  # phase 1
        [[]],  # phase 2
        [[]],  # phase 3
        [[]],  # phase 4
        # phase 5.1: 3 expired accounts
        [
            [
                {"id": 1, "inventory_id": 100},
                {"id": 2, "inventory_id": 101},
                {"id": 3, "inventory_id": 102},
            ]
        ],
        [[]],  # phase 5.2 archive
        [[]],  # phase 5.3 prune
        [[]],  # phase 5.4 block retries
        [[]],  # phase 5.5 unblock retries
    )
    with patch("orchestrator.watchdog.connect", new=fake_connect):
        counters = WatchdogService().run_once()

    assert counters["pergb_accounts_expired"] == 3
    # Phase 5.1 cursor (index 4) should have issued both the traffic_accounts
    # UPDATE and the proxy_inventory cascade UPDATE.
    sqls = [c.args[0] for c in cursors[4].execute.call_args_list]
    assert any("update traffic_accounts" in s and "expired" in s for s in sqls)
    assert any(
        "update proxy_inventory" in s and "expired_grace" in s and "allocated_pergb" in s for s in sqls
    )


def test_phase5_archives_after_grace() -> None:
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],
        [[]],
        [[]],
        [[]],  # phases 1-4
        [[]],  # 5.1: nothing newly expired
        # 5.2: 2 rows past the 3-day grace
        [
            [
                {"id": 10, "inventory_id": 200},
                {"id": 11, "inventory_id": 201},
            ]
        ],
        [[]],  # 5.3 prune
        [[]],  # 5.4 block retries
        [[]],  # 5.5 unblock retries
    )
    with patch("orchestrator.watchdog.connect", new=fake_connect):
        counters = WatchdogService().run_once()

    assert counters["pergb_accounts_archived"] == 2
    sqls = [c.args[0] for c in cursors[5].execute.call_args_list]
    assert any("archived" in s and "update traffic_accounts" in s for s in sqls)
    assert any("update proxy_inventory" in s and "expired_grace" in s for s in sqls)


def test_phase5_prunes_old_samples() -> None:
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],
        [[]],
        [[]],
        [[]],
        [[]],  # 5.1
        [[]],  # 5.2
        # 5.3: 7 sample rows pruned
        [[{"id": i} for i in range(7)]],
        [[]],  # 5.4 block retries
        [[]],  # 5.5 unblock retries
    )
    with patch("orchestrator.watchdog.connect", new=fake_connect):
        counters = WatchdogService().run_once()

    assert counters["pergb_samples_pruned"] == 7
    call = cursors[6].execute.call_args_list[0]
    assert "delete from traffic_samples" in call.args[0]
    # Retention = 30 days — bound as a parameter, not literal in SQL
    assert call.args[1] == (30,)


def test_phase5_no_op_when_clean_does_not_cascade() -> None:
    """When phase 5.1 finds nothing, the cascade UPDATE on proxy_inventory must
    NOT fire — that would needlessly hit the inventory table every cycle."""
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],
        [[]],
        [[]],
        [[]],
        [[]],  # 5.1: empty
        [[]],  # 5.2: empty
        [[]],  # 5.3: empty
        [[]],  # 5.4: empty
        [[]],  # 5.5: empty
    )
    with patch("orchestrator.watchdog.connect", new=fake_connect):
        counters = WatchdogService().run_once()

    assert counters["pergb_accounts_expired"] == 0
    assert counters["pergb_accounts_archived"] == 0
    assert counters["pergb_samples_pruned"] == 0
    # Phase 5.1 should have issued exactly ONE execute (the SELECT/UPDATE-RETURNING),
    # NOT a second cascade execute.
    assert cursors[4].execute.call_count == 1
    assert cursors[5].execute.call_count == 1
