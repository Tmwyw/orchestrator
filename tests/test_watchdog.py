"""Unit tests for WatchdogService.

The watchdog only touches DB; we patch ``orchestrator.watchdog.connect`` with a
fake context manager whose cursors return pre-staged SELECT/RETURNING rows.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _make_cursor(*, fetchall_queue: list[list[dict[str, Any]]]) -> MagicMock:
    cursor = MagicMock(name="cursor")
    cursor.execute = MagicMock()
    cursor.fetchall = MagicMock(side_effect=lambda: fetchall_queue.pop(0) if fetchall_queue else [])
    cursor.fetchone = MagicMock(return_value=None)
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=None)
    return cursor


def _make_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.cursor = MagicMock(return_value=cursor)
    return conn


def _make_phased_connect(*phases: list[dict[str, Any]]):
    """Build a fake ``connect()`` whose successive uses return cursors with the
    given ``fetchall_queue`` contents.

    Each phase corresponds to one ``with connect() as conn`` block in
    watchdog.run_once. For the orders phase (phase 2) extra cursors may be
    obtained from the same conn — those are returned by the same per-phase
    cursor mock (its ``fetchall_queue`` empties after the first SELECT, and
    subsequent ``execute`` calls in the per-order loop need no return value).
    """
    cursors: list[MagicMock] = []
    conns: list[MagicMock] = []
    for queue in phases:
        cur = _make_cursor(fetchall_queue=list(queue))
        cursors.append(cur)
        conns.append(_make_conn(cur))

    iterator = iter(conns)

    @contextmanager
    def fake_connect():
        yield next(iterator)

    return fake_connect, cursors


@pytest.fixture
def _cfg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_RUNNING_TIMEOUT_SEC", "1800")
    monkeypatch.setenv("WATCHDOG_PENDING_VALIDATION_TIMEOUT_SEC", "600")


def test_watchdog_marks_stuck_running_jobs_failed(_cfg: None) -> None:
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[{"id": "j1"}, {"id": "j2"}, {"id": "j3"}]],  # phase 1: jobs
        [[]],  # phase 2: no expired orders
        [[]],  # phase 3: no stale pending validation
        [[]],  # phase 4: no expired delivery
        [[]],  # phase 5.1: no pergb expired
        [[]],  # phase 5.2: no pergb archive
        [[]],  # phase 5.3: no samples pruned
        [[]],  # phase 5.4: no block retries pending
        [[]],  # phase 5.5: no unblock retries pending
    )
    with patch("orchestrator.watchdog.connect", new=fake_connect):
        counters = WatchdogService().run_once()

    assert counters["jobs_failed_running"] == 3
    assert counters["orders_released_expired"] == 0
    sql = cursors[0].execute.call_args[0][0]
    assert "watchdog_running_timeout" in sql
    assert "status = 'failed'" in sql


def test_watchdog_releases_expired_reservations(_cfg: None) -> None:
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],  # phase 1
        [
            [
                {"id": 11, "order_ref": "ord_aaa", "reservation_key": "rk-aaa"},
                {"id": 12, "order_ref": "ord_bbb", "reservation_key": "rk-bbb"},
            ]
        ],  # phase 2: 2 expired orders
        [[]],  # phase 3
        [[]],  # phase 4
        [[]],  # phase 5.1
        [[]],  # phase 5.2
        [[]],  # phase 5.3
        [[]],  # phase 5.4
        [[]],  # phase 5.5
    )
    with patch("orchestrator.watchdog.connect", new=fake_connect):
        counters = WatchdogService().run_once()

    assert counters["orders_released_expired"] == 2
    # cursors[1] is the orders-phase cursor reused across the SELECT and the
    # per-order UPDATE pair (2 orders × 2 statements + 1 SELECT = 5 executes).
    assert cursors[1].execute.call_count == 5
    last_calls = [call[0][0] for call in cursors[1].execute.call_args_list]
    assert any("update orders" in s and "released" in s for s in last_calls)
    assert any("update proxy_inventory" in s and "available" in s for s in last_calls)


def test_watchdog_invalidates_stale_pending_validation(_cfg: None) -> None:
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],  # phase 1
        [[]],  # phase 2
        [[{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]],  # phase 3
        [[]],  # phase 4
        [[]],  # phase 5.1
        [[]],  # phase 5.2
        [[]],  # phase 5.3
        [[]],  # phase 5.4
        [[]],  # phase 5.5
    )
    with patch("orchestrator.watchdog.connect", new=fake_connect):
        counters = WatchdogService().run_once()

    assert counters["inventory_invalidated_stale"] == 5
    sql = cursors[2].execute.call_args[0][0]
    assert "watchdog_pending_validation_timeout" in sql
    assert "status = 'invalid'" in sql


def test_watchdog_clears_expired_delivery_content(_cfg: None) -> None:
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],  # phase 1
        [[]],  # phase 2
        [[]],  # phase 3
        [[{"id": 100}, {"id": 101}]],  # phase 4: 2 cleared
        [[]],  # phase 5.1
        [[]],  # phase 5.2
        [[]],  # phase 5.3
        [[]],  # phase 5.4
        [[]],  # phase 5.5
    )
    with patch("orchestrator.watchdog.connect", new=fake_connect):
        counters = WatchdogService().run_once()

    assert counters["delivery_content_expired"] == 2
    sql = cursors[3].execute.call_args[0][0]
    assert "update delivery_files" in sql
    assert "set content = null" in sql


def test_watchdog_run_once_no_op_when_clean(_cfg: None) -> None:
    from orchestrator.watchdog import WatchdogService

    fake_connect, _ = _make_phased_connect([[]], [[]], [[]], [[]], [[]], [[]], [[]], [[]], [[]])
    with patch("orchestrator.watchdog.connect", new=fake_connect):
        counters = WatchdogService().run_once()

    assert counters == {
        "jobs_failed_running": 0,
        "orders_released_expired": 0,
        "inventory_invalidated_stale": 0,
        "delivery_content_expired": 0,
        "pergb_accounts_expired": 0,
        "pergb_accounts_archived": 0,
        "pergb_samples_pruned": 0,
        "pergb_block_retries_attempted": 0,
        "pergb_block_retries_succeeded": 0,
        "pergb_unblock_retries_attempted": 0,
        "pergb_unblock_retries_succeeded": 0,
    }


# === Wave D safety-net retries ===


def _account_id_row(*, account_id: int) -> dict[str, Any]:
    """Wave PERGB-RFCT-A: phase 5.4/5.5 SELECT only returns account_id;
    the per-port join happens in _fetch_account_ports."""
    return {"account_id": account_id}


def _linked_port_row(
    *,
    port: int = 32001,
    node_id: str = "node-x",
    node_url: str = "http://node-x:8085",
    api_key: str | None = "k1",
) -> dict[str, Any]:
    return {
        "port": port,
        "node_id": node_id,
        "node_url": node_url,
        "node_api_key": api_key,
    }


def test_watchdog_retries_pending_blocks_success(_cfg: None) -> None:
    """Phase 5.4: depleted+!node_blocked → post_disable retried across every
    linked port; on full success node_blocked flips TRUE."""
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],  # 1 jobs
        [[]],  # 2 orders
        [[]],  # 3 pending validation
        [[]],  # 4 delivery
        [[]],  # 5.1 pergb expired
        [[]],  # 5.2 pergb archived
        [[]],  # 5.3 samples pruned
        # 5.4: 1st fetchall = account ids; 2nd fetchall = linked ports for that account.
        [[_account_id_row(account_id=42)], [_linked_port_row(port=32001)]],
        [[]],  # 5.5 unblocks
    )

    disable_calls: list[tuple[Any, ...]] = []

    def fake_post_disable(url, api_key, port):
        disable_calls.append((url, api_key, port))
        return {"action": "killed"}

    with (
        patch("orchestrator.watchdog.connect", new=fake_connect),
        patch("orchestrator.watchdog.node_client.post_disable", side_effect=fake_post_disable),
    ):
        counters = WatchdogService().run_once()

    assert counters["pergb_block_retries_attempted"] == 1
    assert counters["pergb_block_retries_succeeded"] == 1
    assert disable_calls == [("http://node-x:8085", "k1", 32001)]

    # Phase 5.4 cursor saw: account-ids SELECT + linked-ports SELECT + UPDATE
    # (success branch with node_blocked=true).
    sql_calls = [c.args[0] for c in cursors[7].execute.call_args_list]
    assert any("status = 'depleted'" in s and "node_blocked = false" in s for s in sql_calls)
    assert any("from proxy_inventory" in s and "traffic_account_id = %s" in s for s in sql_calls)
    assert any("node_blocked = true" in s and "where id = %s" in s for s in sql_calls)


def test_watchdog_retries_pending_blocks_failure_keeps_flag_false(_cfg: None) -> None:
    """Phase 5.4 with post_disable raising: only last_block_attempt_at stamped,
    node_blocked stays FALSE so a future cycle retries again."""
    from orchestrator.node_client import NodeAgentError
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],
        [[]],
        [[]],
        [[]],
        [[]],
        [[]],
        [[]],
        # 5.4: account ids + linked ports
        [[_account_id_row(account_id=42)], [_linked_port_row(port=32001)]],
        [[]],  # 5.5
    )

    def boom(*a, **kw):
        raise NodeAgentError("disable_failed", status_code=500)

    with (
        patch("orchestrator.watchdog.connect", new=fake_connect),
        patch("orchestrator.watchdog.node_client.post_disable", side_effect=boom),
    ):
        counters = WatchdogService().run_once()

    assert counters["pergb_block_retries_attempted"] == 1
    assert counters["pergb_block_retries_succeeded"] == 0

    sql_calls = [c.args[0] for c in cursors[7].execute.call_args_list]
    # Failure branch: stamp last_block_attempt_at but do NOT set node_blocked.
    assert any("last_block_attempt_at = now()" in s and "node_blocked" not in s for s in sql_calls)


def test_watchdog_retries_pending_unblocks_success(_cfg: None) -> None:
    """Phase 5.5: active+node_blocked=TRUE → post_enable retried; node_blocked flips FALSE."""
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect(
        [[]],
        [[]],
        [[]],
        [[]],
        [[]],
        [[]],
        [[]],
        [[]],  # 5.4 nothing
        # 5.5: account ids + linked ports
        [[_account_id_row(account_id=99)], [_linked_port_row(port=32099)]],
    )

    enable_calls: list[tuple[Any, ...]] = []

    def fake_post_enable(url, api_key, port):
        enable_calls.append((url, api_key, port))
        return {"action": "started"}

    with (
        patch("orchestrator.watchdog.connect", new=fake_connect),
        patch("orchestrator.watchdog.node_client.post_enable", side_effect=fake_post_enable),
    ):
        counters = WatchdogService().run_once()

    assert counters["pergb_unblock_retries_attempted"] == 1
    assert counters["pergb_unblock_retries_succeeded"] == 1
    assert enable_calls == [("http://node-x:8085", "k1", 32099)]

    sql_calls = [c.args[0] for c in cursors[8].execute.call_args_list]
    assert any("status = 'active'" in s and "node_blocked = true" in s for s in sql_calls)
    assert any("from proxy_inventory" in s and "traffic_account_id = %s" in s for s in sql_calls)
    assert any("node_blocked = false" in s and "where id = %s" in s for s in sql_calls)


def test_watchdog_retry_select_uses_throttle_and_limit(_cfg: None) -> None:
    """The retry SELECTs must apply the 5-min throttle and a row LIMIT."""
    from orchestrator.watchdog import WatchdogService

    fake_connect, cursors = _make_phased_connect([[]], [[]], [[]], [[]], [[]], [[]], [[]], [[]], [[]])

    with patch("orchestrator.watchdog.connect", new=fake_connect):
        WatchdogService().run_once()

    block_select_sql = cursors[7].execute.call_args_list[0].args[0]
    assert "status = 'depleted'" in block_select_sql
    assert "node_blocked = false" in block_select_sql
    assert "last_block_attempt_at < now() - (%s || ' minutes')::interval" in block_select_sql
    assert "limit %s" in block_select_sql

    unblock_select_sql = cursors[8].execute.call_args_list[0].args[0]
    assert "status = 'active'" in unblock_select_sql
    assert "node_blocked = true" in unblock_select_sql
    assert "last_unblock_attempt_at < now() - (%s || ' minutes')::interval" in unblock_select_sql
    assert "limit %s" in unblock_select_sql
