"""Unit tests for TrafficPollService (Wave B-8.2 design § 4)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.node_client import NodeAgentError


def _make_cursor(
    *,
    fetchall_queue: list[list[dict[str, Any]]] | None = None,
    fetchone_queue: list[dict[str, Any] | None] | None = None,
) -> MagicMock:
    """Build a MagicMock cursor with deterministic fetchall/fetchone replies."""
    cursor = MagicMock(name="cursor")
    cursor.execute = MagicMock()
    fa_queue = list(fetchall_queue or [])
    fo_queue = list(fetchone_queue or [])
    cursor.fetchall = MagicMock(side_effect=lambda: fa_queue.pop(0) if fa_queue else [])
    cursor.fetchone = MagicMock(side_effect=lambda: fo_queue.pop(0) if fo_queue else None)
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=None)
    return cursor


def _make_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.cursor = MagicMock(return_value=cursor)
    return conn


def _make_phased_connect(*phases: MagicMock):
    """Build a fake ``connect()`` that yields a fresh per-phase conn per ``with``."""
    conns = list(phases)
    iterator = iter(conns)

    @contextmanager
    def fake_connect():
        yield next(iterator)

    return fake_connect


def _account_row(
    *,
    account_id: int,
    inventory_id: int,
    bytes_quota: int,
    bytes_used: int,
    last_in: int | None,
    last_out: int | None,
    node_id: str = "node-x",
    node_url: str = "http://node-x:8085",
    api_key: str | None = "k1",
    port: int = 32001,
    sku_code: str = "sku_pergb_us",
) -> dict[str, Any]:
    return {
        "account_id": account_id,
        "inventory_id": inventory_id,
        "bytes_quota": bytes_quota,
        "bytes_used": bytes_used,
        "last_polled_bytes_in": last_in,
        "last_polled_bytes_out": last_out,
        "node_id": node_id,
        "port": port,
        "node_url": node_url,
        "node_api_key": api_key,
        "sku_code": sku_code,
    }


# === happy path ===


def test_run_once_happy_path_two_accounts_one_node(monkeypatch: pytest.MonkeyPatch) -> None:
    """2 accounts on 1 node, both poll OK, deltas computed."""
    from orchestrator import traffic_poll
    from orchestrator.traffic_poll import TrafficPollService

    fetch_cursor = _make_cursor(
        fetchall_queue=[
            [
                _account_row(
                    account_id=1,
                    inventory_id=10,
                    bytes_quota=1_000_000_000,
                    bytes_used=100,
                    last_in=50,
                    last_out=50,
                    port=32001,
                ),
                _account_row(
                    account_id=2,
                    inventory_id=11,
                    bytes_quota=1_000_000_000,
                    bytes_used=0,
                    last_in=None,
                    last_out=None,
                    port=32002,
                ),
            ]
        ],
    )
    persist1 = _make_cursor()
    persist2 = _make_cursor()
    fake_connect = _make_phased_connect(
        _make_conn(fetch_cursor),
        _make_conn(persist1),
        _make_conn(persist2),
    )

    def fake_get_accounting(url, api_key, ports, timeout_sec=10):
        assert sorted(ports) == [32001, 32002]
        return {
            "32001": {"bytes_in": 200, "bytes_out": 200, "bytes_in6": 0, "bytes_out6": 0},
            "32002": {"bytes_in": 50, "bytes_out": 50, "bytes_in6": 0, "bytes_out6": 0},
        }

    monkeypatch.setattr(traffic_poll.node_client, "get_accounting", fake_get_accounting)
    monkeypatch.setattr(
        traffic_poll.node_client,
        "post_disable",
        lambda *a, **kw: pytest.fail("post_disable should not be called"),
    )

    with patch("orchestrator.traffic_poll.connect", new=fake_connect):
        counters = TrafficPollService().run_once()

    assert counters.accounts_polled == 2
    assert counters.accounts_depleted == 0
    assert counters.accounts_disabled == 0
    assert counters.node_failures == 0
    assert counters.counter_resets_detected == 0
    assert counters.skipped_overlap is False

    # First account: delta computed (200-50)+(200-50) = 300; sample inserted + bytes_used updated.
    sql_calls = [c.args[0] for c in persist1.execute.call_args_list]
    assert any("insert into traffic_samples" in s for s in sql_calls)
    assert any("update traffic_accounts" in s for s in sql_calls)


# === partial response from node ===


def test_run_once_partial_node_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Node returns only 1 of 2 ports → process the present, skip the missing."""
    from orchestrator import traffic_poll
    from orchestrator.traffic_poll import TrafficPollService

    fetch_cursor = _make_cursor(
        fetchall_queue=[
            [
                _account_row(
                    account_id=1,
                    inventory_id=10,
                    bytes_quota=1_000_000_000,
                    bytes_used=0,
                    last_in=0,
                    last_out=0,
                    port=32001,
                ),
                _account_row(
                    account_id=2,
                    inventory_id=11,
                    bytes_quota=1_000_000_000,
                    bytes_used=0,
                    last_in=0,
                    last_out=0,
                    port=32002,
                ),
            ]
        ],
    )
    persist1 = _make_cursor()
    fake_connect = _make_phased_connect(
        _make_conn(fetch_cursor),
        _make_conn(persist1),
        _make_conn(_make_cursor()),
    )

    def fake_get_accounting(url, api_key, ports, timeout_sec=10):
        return {"32001": {"bytes_in": 100, "bytes_out": 100, "bytes_in6": 0, "bytes_out6": 0}}
        # 32002 missing — defensive partial response

    monkeypatch.setattr(traffic_poll.node_client, "get_accounting", fake_get_accounting)
    monkeypatch.setattr(traffic_poll.node_client, "post_disable", lambda *a, **kw: None)

    with patch("orchestrator.traffic_poll.connect", new=fake_connect):
        counters = TrafficPollService().run_once()

    assert counters.accounts_polled == 1
    assert counters.node_failures == 0


# === full node failure ===


def test_run_once_full_node_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """node_client raises → node_failures++, no DB writes for this node."""
    from orchestrator import traffic_poll
    from orchestrator.traffic_poll import TrafficPollService

    fetch_cursor = _make_cursor(
        fetchall_queue=[
            [
                _account_row(
                    account_id=1,
                    inventory_id=10,
                    bytes_quota=1_000_000_000,
                    bytes_used=0,
                    last_in=0,
                    last_out=0,
                    port=32001,
                ),
            ]
        ],
    )
    fake_connect = _make_phased_connect(_make_conn(fetch_cursor))

    def boom(*a, **kw):
        raise NodeAgentError("nft_failed", status_code=503)

    monkeypatch.setattr(traffic_poll.node_client, "get_accounting", boom)

    with patch("orchestrator.traffic_poll.connect", new=fake_connect):
        counters = TrafficPollService().run_once()

    assert counters.node_failures == 1
    assert counters.accounts_polled == 0


# === counter reset ===


def test_run_once_counter_reset_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """delta < 0 → reset_detected=True, delta clamped to 0, anchor re-set."""
    from orchestrator import traffic_poll
    from orchestrator.traffic_poll import TrafficPollService

    fetch_cursor = _make_cursor(
        fetchall_queue=[
            [
                _account_row(
                    account_id=7,
                    inventory_id=10,
                    bytes_quota=1_000_000_000,
                    bytes_used=500,
                    last_in=1000,
                    last_out=2000,
                    port=32001,
                ),
            ]
        ],
    )
    persist = _make_cursor()
    fake_connect = _make_phased_connect(
        _make_conn(fetch_cursor),
        _make_conn(persist),
    )

    # New reading is LOWER than the anchor → counter reset
    def fake_get_accounting(url, api_key, ports, timeout_sec=10):
        return {"32001": {"bytes_in": 10, "bytes_out": 20, "bytes_in6": 0, "bytes_out6": 0}}

    monkeypatch.setattr(traffic_poll.node_client, "get_accounting", fake_get_accounting)

    with patch("orchestrator.traffic_poll.connect", new=fake_connect):
        counters = TrafficPollService().run_once()

    assert counters.counter_resets_detected == 1
    assert counters.accounts_polled == 1

    # The traffic_samples row should have counter_reset_detected=True and zero deltas.
    insert_call = persist.execute.call_args_list[0]
    insert_params = insert_call.args[1]
    # Params order: (account_id, bytes_in_total, bytes_out_total, delta_in, delta_out, reset)
    assert insert_params[3] == 0  # delta_in clamped
    assert insert_params[4] == 0  # delta_out clamped
    assert insert_params[5] is True

    # bytes_used must NOT have grown — anchor moved to new (lower) reading instead.
    update_call = persist.execute.call_args_list[1]
    update_params = update_call.args[1]
    # Params: (new_bytes_used, bytes_in_total, bytes_out_total, account_id)
    assert update_params[0] == 500  # unchanged
    assert update_params[1] == 10
    assert update_params[2] == 20


# === depletion-trigger ===


def test_run_once_depletion_trigger_calls_post_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    """bytes_used + delta >= bytes_quota → status flipped + post_disable called."""
    from orchestrator import traffic_poll
    from orchestrator.traffic_poll import TrafficPollService

    fetch_cursor = _make_cursor(
        fetchall_queue=[
            [
                _account_row(
                    account_id=42,
                    inventory_id=10,
                    bytes_quota=1000,
                    bytes_used=900,
                    last_in=0,
                    last_out=0,
                    port=32001,
                    node_id="node-x",
                    node_url="http://node-x:8085",
                ),
            ]
        ],
    )
    # Persist cursor must return a row from the depletion UPDATE...RETURNING.
    persist = _make_cursor(fetchone_queue=[{"id": 42}])
    fake_connect = _make_phased_connect(
        _make_conn(fetch_cursor),
        _make_conn(persist),
    )

    def fake_get_accounting(url, api_key, ports, timeout_sec=10):
        # Adding 100 + 100 puts bytes_used at 1100, > quota of 1000.
        return {"32001": {"bytes_in": 100, "bytes_out": 100, "bytes_in6": 0, "bytes_out6": 0}}

    disable_calls: list[tuple[Any, ...]] = []

    def fake_post_disable(url, api_key, port, timeout_sec=10):
        disable_calls.append((url, api_key, port))
        return {"action": "killed"}

    monkeypatch.setattr(traffic_poll.node_client, "get_accounting", fake_get_accounting)
    monkeypatch.setattr(traffic_poll.node_client, "post_disable", fake_post_disable)

    with patch("orchestrator.traffic_poll.connect", new=fake_connect):
        counters = TrafficPollService().run_once()

    assert counters.accounts_depleted == 1
    assert counters.accounts_disabled == 1
    assert disable_calls == [("http://node-x:8085", "k1", 32001)]

    # Verify the depletion UPDATE was issued.
    update_calls = [c.args[0] for c in persist.execute.call_args_list]
    assert any("status = 'depleted'" in s and "depleted_at" in s for s in update_calls)


def test_run_once_depletion_disable_failure_is_logged_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """post_disable raising must not abort the cycle — next cycle retries naturally."""
    from orchestrator import traffic_poll
    from orchestrator.traffic_poll import TrafficPollService

    fetch_cursor = _make_cursor(
        fetchall_queue=[
            [
                _account_row(
                    account_id=42,
                    inventory_id=10,
                    bytes_quota=1000,
                    bytes_used=900,
                    last_in=0,
                    last_out=0,
                    port=32001,
                ),
            ]
        ],
    )
    persist = _make_cursor(fetchone_queue=[{"id": 42}])
    fake_connect = _make_phased_connect(
        _make_conn(fetch_cursor),
        _make_conn(persist),
    )

    monkeypatch.setattr(
        traffic_poll.node_client,
        "get_accounting",
        lambda *a, **kw: {"32001": {"bytes_in": 100, "bytes_out": 100, "bytes_in6": 0, "bytes_out6": 0}},
    )

    def boom_disable(*a, **kw):
        raise NodeAgentError("disable_failed", status_code=500)

    monkeypatch.setattr(traffic_poll.node_client, "post_disable", boom_disable)

    with patch("orchestrator.traffic_poll.connect", new=fake_connect):
        counters = TrafficPollService().run_once()

    # Still flipped to depleted; just not reported as disabled.
    assert counters.accounts_depleted == 1
    assert counters.accounts_disabled == 0


# === top-up reactivation: anchor preserved ===


def test_run_once_polls_reactivated_account_with_preserved_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a top-up, account.status is 'active' again with the anchor still in
    place. Polling must compute delta vs the pre-topup anchor as usual."""
    from orchestrator import traffic_poll
    from orchestrator.traffic_poll import TrafficPollService

    fetch_cursor = _make_cursor(
        fetchall_queue=[
            [
                _account_row(
                    account_id=99,
                    inventory_id=10,
                    bytes_quota=10_000,  # post-topup quota
                    bytes_used=900,  # carried over
                    last_in=400,
                    last_out=500,  # anchor preserved across reactivation
                    port=32001,
                ),
            ]
        ],
    )
    persist = _make_cursor()
    fake_connect = _make_phased_connect(
        _make_conn(fetch_cursor),
        _make_conn(persist),
    )

    monkeypatch.setattr(
        traffic_poll.node_client,
        "get_accounting",
        lambda *a, **kw: {"32001": {"bytes_in": 600, "bytes_out": 700, "bytes_in6": 0, "bytes_out6": 0}},
    )

    with patch("orchestrator.traffic_poll.connect", new=fake_connect):
        counters = TrafficPollService().run_once()

    assert counters.accounts_polled == 1
    assert counters.counter_resets_detected == 0
    # delta_in=200 (600-400), delta_out=200 (700-500) → new bytes_used=1300
    update_params = persist.execute.call_args_list[1].args[1]
    assert update_params[0] == 1300


# === serialization gate ===


def test_run_once_serialization_gate_skips_overlap() -> None:
    """If the lock is already held, run_once returns skipped_overlap=True."""
    from orchestrator.traffic_poll import TrafficPollService

    service = TrafficPollService()
    # Manually grab the lock — simulates a still-running prior cycle.
    service._lock.acquire()
    try:
        counters = service.run_once()
    finally:
        service._lock.release()

    assert counters.skipped_overlap is True
    assert counters.accounts_polled == 0


# === node degrade after threshold ===


def test_run_once_marks_node_degraded_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N=traffic_poll_degrade_after consecutive failures → UPDATE nodes degraded."""
    from orchestrator import traffic_poll
    from orchestrator.traffic_poll import TrafficPollService

    monkeypatch.setenv("TRAFFIC_POLL_DEGRADE_AFTER", "3")

    boom = MagicMock(side_effect=NodeAgentError("nft_failed", status_code=503))
    monkeypatch.setattr(traffic_poll.node_client, "get_accounting", boom)

    service = TrafficPollService()

    # Each cycle: one fetch_active_accounts conn (returns the same single account)
    # and on the 3rd cycle one extra conn for _mark_node_degraded.
    def make_fetch_cursor():
        return _make_cursor(
            fetchall_queue=[
                [
                    _account_row(
                        account_id=1,
                        inventory_id=10,
                        bytes_quota=1_000_000_000,
                        bytes_used=0,
                        last_in=0,
                        last_out=0,
                        port=32001,
                        node_id="node-degrade-test",
                    ),
                ]
            ],
        )

    # Cycle 1
    fc1 = _make_phased_connect(_make_conn(make_fetch_cursor()))
    with patch("orchestrator.traffic_poll.connect", new=fc1):
        c1 = service.run_once()
    assert c1.node_failures == 1

    # Cycle 2
    fc2 = _make_phased_connect(_make_conn(make_fetch_cursor()))
    with patch("orchestrator.traffic_poll.connect", new=fc2):
        c2 = service.run_once()
    assert c2.node_failures == 1

    # Cycle 3 — must trigger degrade (UPDATE nodes RETURNING id) — fetchone returns the row
    degrade_cursor = _make_cursor(fetchone_queue=[{"id": "node-degrade-test"}])
    fc3 = _make_phased_connect(
        _make_conn(make_fetch_cursor()),
        _make_conn(degrade_cursor),
    )
    with patch("orchestrator.traffic_poll.connect", new=fc3):
        c3 = service.run_once()
    assert c3.node_failures == 1

    # The degrade UPDATE was issued.
    sql = degrade_cursor.execute.call_args.args[0]
    assert "update nodes" in sql
    assert "runtime_status = 'degraded'" in sql


def test_consecutive_failures_reset_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """One success between failures resets the counter — degrade only fires on
    *consecutive* failures."""
    from orchestrator import traffic_poll
    from orchestrator.traffic_poll import TrafficPollService

    monkeypatch.setenv("TRAFFIC_POLL_DEGRADE_AFTER", "3")

    service = TrafficPollService()

    def make_fetch_cursor():
        return _make_cursor(
            fetchall_queue=[
                [
                    _account_row(
                        account_id=1,
                        inventory_id=10,
                        bytes_quota=1_000_000_000,
                        bytes_used=0,
                        last_in=0,
                        last_out=0,
                        port=32001,
                        node_id="node-flap",
                    ),
                ]
            ],
        )

    # Fail twice
    monkeypatch.setattr(
        traffic_poll.node_client,
        "get_accounting",
        MagicMock(side_effect=NodeAgentError("nft_failed", status_code=503)),
    )
    for _ in range(2):
        with patch(
            "orchestrator.traffic_poll.connect",
            new=_make_phased_connect(_make_conn(make_fetch_cursor())),
        ):
            service.run_once()

    # Success — resets counter
    monkeypatch.setattr(
        traffic_poll.node_client,
        "get_accounting",
        lambda *a, **kw: {"32001": {"bytes_in": 0, "bytes_out": 0, "bytes_in6": 0, "bytes_out6": 0}},
    )
    with patch(
        "orchestrator.traffic_poll.connect",
        new=_make_phased_connect(
            _make_conn(make_fetch_cursor()),
            _make_conn(_make_cursor()),  # persist
        ),
    ):
        service.run_once()

    # Now fail again twice — would normally hit threshold, but counter was reset
    monkeypatch.setattr(
        traffic_poll.node_client,
        "get_accounting",
        MagicMock(side_effect=NodeAgentError("nft_failed", status_code=503)),
    )
    for _ in range(2):
        with patch(
            "orchestrator.traffic_poll.connect",
            new=_make_phased_connect(_make_conn(make_fetch_cursor())),
        ):
            service.run_once()

    # No degrade should have happened — _node_failures should be 2, not 4
    assert service._node_failures["node-flap"] == 2


def test_run_once_no_active_accounts_returns_zero_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty traffic_accounts table → fast no-op."""
    from orchestrator.traffic_poll import TrafficPollService

    fetch_cursor = _make_cursor(fetchall_queue=[[]])
    fake_connect = _make_phased_connect(_make_conn(fetch_cursor))

    with patch("orchestrator.traffic_poll.connect", new=fake_connect):
        counters = TrafficPollService().run_once()

    assert counters.accounts_polled == 0
    assert counters.skipped_overlap is False
