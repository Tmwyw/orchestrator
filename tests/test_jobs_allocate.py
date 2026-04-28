"""Smoke tests for orchestrator.jobs.allocate_port_range_via_table.

Hotfix verification: the function must use ``pg_advisory_xact_lock`` (not
``FOR UPDATE``) before reading ``MAX(end_port)``, since FOR UPDATE cannot be
combined with aggregate functions in PostgreSQL.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


def _make_config(*, start_port_min: int = 32000, start_port_max: int = 65000) -> Any:
    cfg = MagicMock()
    cfg.start_port_min = start_port_min
    cfg.start_port_max = start_port_max
    return cfg


def _make_conn(max_end: int) -> tuple[Any, MagicMock]:
    """Return (conn-with-cursor-context-manager, cursor mock)."""
    cur = MagicMock()
    cur.fetchone.return_value = {"max_end": max_end}
    cur_cm = MagicMock()
    cur_cm.__enter__ = MagicMock(return_value=cur)
    cur_cm.__exit__ = MagicMock(return_value=False)
    conn = MagicMock()
    conn.cursor.return_value = cur_cm
    return conn, cur


def test_allocate_port_range_uses_advisory_lock() -> None:
    from orchestrator.jobs import allocate_port_range_via_table

    conn, cur = _make_conn(max_end=0)
    with patch("orchestrator.jobs.get_config", return_value=_make_config()):
        start, end = allocate_port_range_via_table(conn, node_id="node-1", job_id="job-abc", count=10)

    assert start == 32000
    assert end == 32009

    assert cur.execute.call_count == 3
    first_sql = cur.execute.call_args_list[0].args[0]
    assert "pg_advisory_xact_lock" in first_sql

    second_sql = cur.execute.call_args_list[1].args[0]
    assert "max(end_port)" in second_sql
    assert "for update" not in second_sql.lower()

    third_sql = cur.execute.call_args_list[2].args[0]
    assert "insert into node_port_allocations" in third_sql


def test_allocate_port_range_continues_after_existing_allocations() -> None:
    from orchestrator.jobs import allocate_port_range_via_table

    conn, cur = _make_conn(max_end=33000)
    with patch("orchestrator.jobs.get_config", return_value=_make_config()):
        start, end = allocate_port_range_via_table(conn, node_id="node-1", job_id="job-xyz", count=5)
    assert start == 33001
    assert end == 33005


def test_allocate_port_range_raises_on_capacity_exhausted() -> None:
    from orchestrator.jobs import allocate_port_range_via_table

    # max_end is 1 short of the upper bound; count=10 would overflow.
    conn, _cur = _make_conn(max_end=64995)
    with (
        patch("orchestrator.jobs.get_config", return_value=_make_config()),
        # Expect RuntimeError before any INSERT; we don't assert on inserts
        # — the failure path raises out of the with-block.
    ):
        try:
            allocate_port_range_via_table(conn, node_id="node-1", job_id="job-overflow", count=10)
        except RuntimeError as exc:
            assert str(exc) == "capacity_not_available"
        else:
            raise AssertionError("expected RuntimeError('capacity_not_available')")


def test_allocate_port_range_rejects_non_positive_count() -> None:
    from orchestrator.jobs import allocate_port_range_via_table

    conn, _cur = _make_conn(max_end=0)
    try:
        allocate_port_range_via_table(conn, node_id="node-1", job_id="j", count=0)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for count <= 0")
