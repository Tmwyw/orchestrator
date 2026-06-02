"""Unit tests for RefillService — Wave POOL-PER-NODE.A per-binding logic.

Stock is per-node: each active binding is topped up to its OWN
``target_stock`` independently. These tests mock the DB layer (``connect``,
the private ``_list_*`` / ``_count_*`` / ``_insert_*`` helpers) and the
side-effecting ``allocate_port_range_via_table``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import MagicMock, patch


def _make_config(
    *,
    proxy_allow_degraded_nodes: bool = False,
    refill_default_priority: int = 10,
    refill_max_skus_per_cycle: int = 100,
) -> Any:
    cfg = MagicMock()
    cfg.proxy_allow_degraded_nodes = proxy_allow_degraded_nodes
    cfg.refill_default_priority = refill_default_priority
    cfg.refill_max_skus_per_cycle = refill_max_skus_per_cycle
    return cfg


@contextmanager
def _fake_connect():
    yield MagicMock(name="conn")


def _make_service_with_mocks(
    *,
    skus: list[dict[str, Any]],
    bindings_by_sku: dict[int, list[dict[str, Any]]],
    available_by_node: dict[str, int] | None = None,
    in_flight_by_node: dict[str, int] | None = None,
) -> Any:
    from orchestrator.refill import RefillService

    service = RefillService()
    service._list_active_skus = MagicMock(return_value=skus)  # type: ignore[method-assign]
    service._list_active_bindings_with_capacity = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda conn, *, sku_id, allow_degraded: bindings_by_sku.get(sku_id, [])
    )
    avail = available_by_node or {}
    service._count_available_on_node = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda conn, *, sku_id, node_id: avail.get(node_id, 0)
    )
    in_flight = in_flight_by_node or {}
    service._count_in_flight_jobs = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda conn, *, node_id: in_flight.get(node_id, 0)
    )
    service._insert_refill_job = MagicMock(return_value=None)  # type: ignore[method-assign]
    service._set_job_start_port = MagicMock(return_value=None)  # type: ignore[method-assign]
    return service


def _sku(
    *,
    sku_id: int = 1,
    code: str = "ipv6-us-30d",
    product_kind: str = "ipv6",
    protocol: str = "socks5",
    geo_code: str = "US",
    require_ipv6: bool = True,
) -> dict[str, Any]:
    return {
        "id": sku_id,
        "code": code,
        "product_kind": product_kind,
        "protocol": protocol,
        "geo_code": geo_code,
        "validation_require_ipv6": require_ipv6,
    }


def _binding(
    *,
    node_id: str,
    sku_id: int = 1,
    target_stock: int = 4000,
    effective_max_batch: int = 1500,
    max_parallel_jobs: int = 2,
    runtime_status: str = "active",
) -> dict[str, Any]:
    return {
        "sku_id": sku_id,
        "node_id": node_id,
        "binding_weight": 100,
        "target_stock": target_stock,
        "effective_max_batch": effective_max_batch,
        "max_parallel_jobs": max_parallel_jobs,
        "runtime_status": runtime_status,
    }


def _run(service):
    with (
        patch("orchestrator.refill.connect", _fake_connect),
        patch("orchestrator.refill.get_config", return_value=_make_config()),
        patch(
            "orchestrator.refill.allocate_port_range_via_table",
            return_value=(32000, 32999),
        ) as alloc,
    ):
        counters = service.run_once()
    return counters, alloc


# ── single node ──────────────────────────────────────────────────


def test_refill_single_node_tops_up_to_its_target() -> None:
    """1 node target 4000, available 2500 → generate 1500 on that node."""
    service = _make_service_with_mocks(
        skus=[_sku()],
        bindings_by_sku={1: [_binding(node_id="n1", target_stock=4000, effective_max_batch=10000)]},
        available_by_node={"n1": 2500},
    )
    counters, alloc = _run(service)

    assert counters["bindings_with_deficit"] == 1
    assert counters["jobs_enqueued"] == 1
    insert = service._insert_refill_job.call_args.kwargs
    assert insert["node_id"] == "n1"
    assert insert["count"] == 1500
    assert insert["product"] == "android_ipv6_only"
    alloc.assert_called_once()
    service._set_job_start_port.assert_called_once()


def test_refill_single_node_no_deficit_skips() -> None:
    """available >= target → nothing scheduled."""
    service = _make_service_with_mocks(
        skus=[_sku()],
        bindings_by_sku={1: [_binding(node_id="n1", target_stock=4000)]},
        available_by_node={"n1": 4000},
    )
    counters, alloc = _run(service)

    assert counters["bindings_processed"] == 1
    assert counters["bindings_with_deficit"] == 0
    assert counters["jobs_enqueued"] == 0
    service._insert_refill_job.assert_not_called()
    alloc.assert_not_called()


# ── multi node: each independent ──────────────────────────────────


def test_refill_three_nodes_each_to_own_target() -> None:
    """Nodes with targets 4000/3000/5000, all empty → each fills to its own
    target (big batch cap)."""
    service = _make_service_with_mocks(
        skus=[_sku()],
        bindings_by_sku={
            1: [
                _binding(node_id="a", target_stock=4000, effective_max_batch=10000),
                _binding(node_id="b", target_stock=3000, effective_max_batch=10000),
                _binding(node_id="c", target_stock=5000, effective_max_batch=10000),
            ]
        },
        available_by_node={"a": 0, "b": 0, "c": 0},
    )
    counters, _ = _run(service)

    assert counters["jobs_enqueued"] == 3
    by_node = {
        call.kwargs["node_id"]: call.kwargs["count"]
        for call in service._insert_refill_job.call_args_list
    }
    assert by_node == {"a": 4000, "b": 3000, "c": 5000}


def test_refill_mixed_deficits_only_short_nodes_fill() -> None:
    service = _make_service_with_mocks(
        skus=[_sku()],
        bindings_by_sku={
            1: [
                _binding(node_id="full", target_stock=4000, effective_max_batch=10000),
                _binding(node_id="short", target_stock=4000, effective_max_batch=10000),
            ]
        },
        available_by_node={"full": 4000, "short": 1000},
    )
    counters, _ = _run(service)

    assert counters["jobs_enqueued"] == 1
    insert = service._insert_refill_job.call_args.kwargs
    assert insert["node_id"] == "short"
    assert insert["count"] == 3000


# ── target 0 → never generates ───────────────────────────────────


def test_refill_node_with_zero_target_skipped() -> None:
    service = _make_service_with_mocks(
        skus=[_sku()],
        bindings_by_sku={1: [_binding(node_id="n1", target_stock=0)]},
        available_by_node={"n1": 0},
    )
    counters, alloc = _run(service)

    assert counters["bindings_processed"] == 1
    assert counters["bindings_with_deficit"] == 0
    assert counters["jobs_enqueued"] == 0
    service._insert_refill_job.assert_not_called()
    alloc.assert_not_called()


# ── per-cycle batch cap ──────────────────────────────────────────


def test_refill_deficit_capped_by_effective_max_batch() -> None:
    """Deficit 4000 but batch cap 500 → schedule 500 this cycle."""
    service = _make_service_with_mocks(
        skus=[_sku()],
        bindings_by_sku={1: [_binding(node_id="n1", target_stock=4000, effective_max_batch=500)]},
        available_by_node={"n1": 0},
    )
    _run(service)

    assert service._insert_refill_job.call_args.kwargs["count"] == 500


# ── in-flight gate (runaway guard) ───────────────────────────────


def test_refill_node_at_capacity_skipped() -> None:
    service = _make_service_with_mocks(
        skus=[_sku()],
        bindings_by_sku={
            1: [
                _binding(node_id="busy", target_stock=4000, max_parallel_jobs=2),
                _binding(node_id="free", target_stock=4000, max_parallel_jobs=2),
            ]
        },
        available_by_node={"busy": 0, "free": 0},
        in_flight_by_node={"busy": 2, "free": 0},
    )
    counters, _ = _run(service)

    assert counters["nodes_at_capacity"] == 1
    assert counters["jobs_enqueued"] == 1
    nodes = [c.kwargs["node_id"] for c in service._insert_refill_job.call_args_list]
    assert nodes == ["free"]


# ── no bindings ──────────────────────────────────────────────────


def test_refill_sku_without_bindings_skipped() -> None:
    service = _make_service_with_mocks(
        skus=[_sku()],
        bindings_by_sku={1: []},
    )
    counters, alloc = _run(service)

    assert counters["skus_processed"] == 1
    assert counters["bindings_processed"] == 0
    assert counters["bindings_with_deficit"] == 0
    assert counters["jobs_enqueued"] == 0
    service._insert_refill_job.assert_not_called()
    alloc.assert_not_called()
