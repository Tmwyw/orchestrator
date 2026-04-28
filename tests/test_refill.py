"""Unit tests for RefillService: deficit logic, distribution, in-flight gating.

These tests exercise pure-Python decision logic by mocking the DB layer
(``connect``, the private ``_list_*`` / ``_get_*`` / ``_count_*`` /
``_insert_*`` helpers) and the side-effecting ``allocate_port_range_via_table``.
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
    projection_by_sku: dict[int, dict[str, int]],
    bindings_by_sku: dict[int, list[dict[str, Any]]],
    in_flight_by_node: dict[str, int] | None = None,
) -> Any:
    from orchestrator.refill import RefillService

    service = RefillService()
    service._list_active_skus = MagicMock(return_value=skus)  # type: ignore[method-assign]
    service._get_sku_projection = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda conn, sku_id: projection_by_sku[sku_id]
    )
    service._list_active_bindings_with_capacity = MagicMock(  # type: ignore[method-assign]
        side_effect=lambda conn, *, sku_id, allow_degraded: bindings_by_sku.get(sku_id, [])
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
    target_stock: int = 1000,
    refill_batch_size: int = 500,
    product_kind: str = "ipv6",
    protocol: str = "socks5",
    geo_code: str = "US",
    require_ipv6: bool = True,
) -> dict[str, Any]:
    return {
        "id": sku_id,
        "code": code,
        "target_stock": target_stock,
        "refill_batch_size": refill_batch_size,
        "product_kind": product_kind,
        "protocol": protocol,
        "geo_code": geo_code,
        "validation_require_ipv6": require_ipv6,
    }


def _binding(
    *,
    node_id: str,
    sku_id: int = 1,
    effective_max_batch: int = 1500,
    max_parallel_jobs: int = 2,
    runtime_status: str = "active",
) -> dict[str, Any]:
    return {
        "sku_id": sku_id,
        "node_id": node_id,
        "binding_weight": 100,
        "effective_max_batch": effective_max_batch,
        "max_parallel_jobs": max_parallel_jobs,
        "runtime_status": runtime_status,
    }


def test_refill_skips_skus_without_deficit() -> None:
    service = _make_service_with_mocks(
        skus=[_sku(sku_id=1, target_stock=1000)],
        projection_by_sku={1: {"available": 1000, "pending_validation": 0, "queued_or_running": 0}},
        bindings_by_sku={1: [_binding(node_id="n1")]},
    )
    with (
        patch("orchestrator.refill.connect", _fake_connect),
        patch("orchestrator.refill.get_config", return_value=_make_config()),
        patch("orchestrator.refill.allocate_port_range_via_table") as alloc,
    ):
        counters = service.run_once()

    assert counters["skus_processed"] == 1
    assert counters["skus_with_deficit"] == 0
    assert counters["jobs_enqueued"] == 0
    service._insert_refill_job.assert_not_called()
    alloc.assert_not_called()


def test_refill_enqueues_for_deficit_with_single_node() -> None:
    service = _make_service_with_mocks(
        skus=[_sku(sku_id=1, target_stock=1000, refill_batch_size=500)],
        projection_by_sku={1: {"available": 500, "pending_validation": 0, "queued_or_running": 0}},
        bindings_by_sku={1: [_binding(node_id="n1", effective_max_batch=10000)]},
    )
    with (
        patch("orchestrator.refill.connect", _fake_connect),
        patch("orchestrator.refill.get_config", return_value=_make_config()),
        patch("orchestrator.refill.allocate_port_range_via_table", return_value=(32000, 32499)) as alloc,
    ):
        counters = service.run_once()

    assert counters["skus_with_deficit"] == 1
    assert counters["jobs_enqueued"] == 1
    assert service._insert_refill_job.call_count == 1
    insert_kwargs = service._insert_refill_job.call_args.kwargs
    assert insert_kwargs["sku_id"] == 1
    assert insert_kwargs["node_id"] == "n1"
    assert insert_kwargs["count"] == 500
    assert insert_kwargs["product"] == "android_ipv6_only"
    alloc.assert_called_once()
    service._set_job_start_port.assert_called_once()


def test_refill_distributes_equally_across_4_nodes() -> None:
    bindings = [_binding(node_id=f"n{i}", effective_max_batch=10000) for i in range(1, 5)]
    service = _make_service_with_mocks(
        skus=[_sku(sku_id=1, target_stock=2000, refill_batch_size=1000)],
        projection_by_sku={1: {"available": 0, "pending_validation": 0, "queued_or_running": 0}},
        bindings_by_sku={1: bindings},
    )
    with (
        patch("orchestrator.refill.connect", _fake_connect),
        patch("orchestrator.refill.get_config", return_value=_make_config()),
        patch("orchestrator.refill.allocate_port_range_via_table", return_value=(32000, 32249)),
    ):
        counters = service.run_once()

    assert counters["jobs_enqueued"] == 4
    counts = [call.kwargs["count"] for call in service._insert_refill_job.call_args_list]
    assert counts == [250, 250, 250, 250]
    assert sum(counts) == 1000


def test_refill_skips_node_at_capacity() -> None:
    service = _make_service_with_mocks(
        skus=[_sku(sku_id=1, target_stock=1000, refill_batch_size=400)],
        projection_by_sku={1: {"available": 0, "pending_validation": 0, "queued_or_running": 0}},
        bindings_by_sku={
            1: [
                _binding(node_id="full", effective_max_batch=10000, max_parallel_jobs=2),
                _binding(node_id="free", effective_max_batch=10000, max_parallel_jobs=2),
            ]
        },
        in_flight_by_node={"full": 2, "free": 0},
    )
    with (
        patch("orchestrator.refill.connect", _fake_connect),
        patch("orchestrator.refill.get_config", return_value=_make_config()),
        patch("orchestrator.refill.allocate_port_range_via_table", return_value=(32000, 32199)),
    ):
        counters = service.run_once()

    assert counters["nodes_at_capacity"] == 1
    assert counters["jobs_enqueued"] == 1
    enqueued_nodes = [call.kwargs["node_id"] for call in service._insert_refill_job.call_args_list]
    assert enqueued_nodes == ["free"]


def test_refill_respects_refill_batch_size() -> None:
    service = _make_service_with_mocks(
        skus=[_sku(sku_id=1, target_stock=10000, refill_batch_size=500)],
        projection_by_sku={1: {"available": 0, "pending_validation": 0, "queued_or_running": 0}},
        bindings_by_sku={1: [_binding(node_id="n1", effective_max_batch=10000)]},
    )
    with (
        patch("orchestrator.refill.connect", _fake_connect),
        patch("orchestrator.refill.get_config", return_value=_make_config()),
        patch("orchestrator.refill.allocate_port_range_via_table", return_value=(32000, 32499)),
    ):
        service.run_once()

    insert_kwargs = service._insert_refill_job.call_args.kwargs
    assert insert_kwargs["count"] == 500


def test_refill_handles_no_bindings() -> None:
    service = _make_service_with_mocks(
        skus=[_sku(sku_id=1, target_stock=1000)],
        projection_by_sku={1: {"available": 0, "pending_validation": 0, "queued_or_running": 0}},
        bindings_by_sku={1: []},
    )
    with (
        patch("orchestrator.refill.connect", _fake_connect),
        patch("orchestrator.refill.get_config", return_value=_make_config()),
        patch("orchestrator.refill.allocate_port_range_via_table") as alloc,
    ):
        counters = service.run_once()

    assert counters["skus_with_deficit"] == 1
    assert counters["jobs_enqueued"] == 0
    service._insert_refill_job.assert_not_called()
    alloc.assert_not_called()
