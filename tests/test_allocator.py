"""Unit tests for AllocatorService — without real DB or Redis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


def _make_config(
    *,
    proxy_allow_degraded_nodes: bool = False,
    reservation_min_ttl_sec: int = 30,
    reservation_max_ttl_sec: int = 3600,
    reservation_default_ttl_sec: int = 300,
) -> Any:
    cfg = MagicMock()
    cfg.proxy_allow_degraded_nodes = proxy_allow_degraded_nodes
    cfg.reservation_min_ttl_sec = reservation_min_ttl_sec
    cfg.reservation_max_ttl_sec = reservation_max_ttl_sec
    cfg.reservation_default_ttl_sec = reservation_default_ttl_sec
    return cfg


def _binding(node_id: str, sku_id: int = 1) -> dict[str, Any]:
    return {
        "sku_id": sku_id,
        "node_id": node_id,
        "binding_weight": 100,
        "effective_max_batch": 1500,
        "max_parallel_jobs": 2,
        "runtime_status": "active",
    }


def _sku(
    *,
    sku_id: int = 1,
    code: str = "ipv6-us-30d",
    duration_days: int = 30,
    is_active: bool = True,
) -> dict[str, Any]:
    return {
        "id": sku_id,
        "code": code,
        "product_kind": "ipv6",
        "geo_code": "US",
        "protocol": "socks5",
        "duration_days": duration_days,
        "is_active": is_active,
        "target_stock": 1000,
        "refill_batch_size": 500,
        "validation_require_ipv6": True,
    }


async def test_reserve_inactive_sku_returns_error() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    service._sync_get_active_sku = MagicMock(return_value=None)  # type: ignore[method-assign]

    with (
        patch("orchestrator.allocator.get_config", return_value=_make_config()),
        patch("orchestrator.allocator.get_redis", new=AsyncMock(return_value=AsyncMock())),
    ):
        result = await service.reserve(user_id=1, sku_id=99, quantity=10, reservation_ttl_sec=300)
    assert result.success is False
    assert result.error == "sku_not_active"
    assert result.order_ref is None


async def test_reserve_no_bindings_returns_error() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    service._sync_get_active_sku = MagicMock(return_value=_sku())  # type: ignore[method-assign]
    service._sync_list_active_bindings = MagicMock(return_value=[])  # type: ignore[method-assign]

    with (
        patch("orchestrator.allocator.get_config", return_value=_make_config()),
        patch("orchestrator.allocator.get_redis", new=AsyncMock(return_value=AsyncMock())),
    ):
        result = await service.reserve(user_id=1, sku_id=1, quantity=10, reservation_ttl_sec=300)
    assert result.success is False
    assert result.error == "no_active_bindings"


async def test_reserve_insufficient_stock_returns_available_now() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    service._sync_get_active_sku = MagicMock(return_value=_sku())  # type: ignore[method-assign]
    service._sync_list_active_bindings = MagicMock(  # type: ignore[method-assign]
        return_value=[_binding("n1"), _binding("n2"), _binding("n3"), _binding("n4")]
    )
    # Claim only 500 of requested 1000
    service._sync_claim_per_node_with_rollback = MagicMock(  # type: ignore[method-assign]
        return_value=([1, 2, 3], 500)
    )
    service._sync_release_inventory = MagicMock(return_value=500)  # type: ignore[method-assign]
    service._sync_count_available = MagicMock(return_value=500)  # type: ignore[method-assign]

    with (
        patch("orchestrator.allocator.get_config", return_value=_make_config()),
        patch("orchestrator.allocator.get_redis", new=AsyncMock(return_value=AsyncMock())),
    ):
        result = await service.reserve(user_id=1, sku_id=1, quantity=1000, reservation_ttl_sec=300)

    assert result.success is False
    assert result.error == "insufficient_stock"
    assert result.available_now == 500
    service._sync_release_inventory.assert_called_once()


async def test_reserve_success_writes_redis() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    service._sync_get_active_sku = MagicMock(return_value=_sku())  # type: ignore[method-assign]
    service._sync_list_active_bindings = MagicMock(  # type: ignore[method-assign]
        return_value=[_binding("n1"), _binding("n2")]
    )
    service._sync_claim_per_node_with_rollback = MagicMock(  # type: ignore[method-assign]
        return_value=(list(range(1, 1001)), 1000)
    )
    service._sync_insert_order = MagicMock(return_value=None)  # type: ignore[method-assign]

    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock()

    with (
        patch("orchestrator.allocator.get_config", return_value=_make_config()),
        patch("orchestrator.allocator.get_redis", new=AsyncMock(return_value=fake_redis)),
        patch("orchestrator.allocator._sync_next_order_ref", return_value="order_42"),
    ):
        result = await service.reserve(user_id=42, sku_id=1, quantity=1000, reservation_ttl_sec=300)

    assert result.success is True
    # Wave PERGB-INFINITE: new sequential `order_<N>` shape (migration 029)
    # replaces the legacy `ord_<hex>` prefix.
    assert result.order_ref == "order_42"
    assert result.proxies_count == 1000
    fake_redis.set.assert_called_once()
    set_args, set_kwargs = fake_redis.set.call_args
    assert set_args[0] == "reservation:order_42"
    assert set_kwargs.get("ex") == 300


async def test_reserve_idempotency_returns_cached() -> None:
    from orchestrator.allocator import AllocatorService, ReserveResult

    service = AllocatorService()
    cached_result = ReserveResult(
        success=True,
        order_ref="ord_cached12345",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=200),
        proxies_count=100,
    )
    service._idem_get = AsyncMock(return_value=cached_result)  # type: ignore[method-assign]
    service._sync_get_active_sku = MagicMock(
        return_value=None
    )  # would error if called  # type: ignore[method-assign]

    with patch("orchestrator.allocator.get_config", return_value=_make_config()):
        result = await service.reserve(
            user_id=1,
            sku_id=1,
            quantity=100,
            reservation_ttl_sec=300,
            idempotency_key="user-42-key-abc",
        )

    assert result.order_ref == "ord_cached12345"
    assert result.proxies_count == 100
    service._sync_get_active_sku.assert_not_called()


async def test_commit_after_reserve_marks_inventory_sold() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    future = datetime.now(timezone.utc) + timedelta(seconds=200)
    order_row = {
        "id": 17,
        "order_ref": "ord_test12345678",
        "user_id": 42,
        "sku_id": 1,
        "status": "reserved",
        "expires_at": future,
        "reservation_key": "resv_xyz",
    }
    updated_order = {
        **order_row,
        "status": "committed",
        "proxies_expires_at": future + timedelta(days=30),
    }
    service._sync_get_order = MagicMock(return_value=order_row)  # type: ignore[method-assign]
    service._sync_get_sku_any = MagicMock(return_value=_sku())  # type: ignore[method-assign]
    service._sync_commit_order = MagicMock(return_value=updated_order)  # type: ignore[method-assign]

    fake_redis = AsyncMock()
    fake_redis.delete = AsyncMock()

    with patch("orchestrator.allocator.get_redis", new=AsyncMock(return_value=fake_redis)):
        result = await service.commit(order_ref="ord_test12345678", duration_days=None)

    assert result.success is True
    assert result.proxies_expires_at == updated_order["proxies_expires_at"]
    fake_redis.delete.assert_called_once_with("reservation:ord_test12345678")


async def test_commit_expired_reservation_returns_error() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    service._sync_get_order = MagicMock(  # type: ignore[method-assign]
        return_value={
            "id": 18,
            "order_ref": "ord_expired12345",
            "sku_id": 1,
            "status": "reserved",
            "expires_at": past,
            "reservation_key": "resv_old",
        }
    )

    result = await service.commit(order_ref="ord_expired12345", duration_days=None)
    assert result.success is False
    assert result.error == "reservation_expired"


async def test_release_marks_inventory_available() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    future = datetime.now(timezone.utc) + timedelta(seconds=200)
    order_row = {
        "id": 21,
        "order_ref": "ord_release1234",
        "sku_id": 1,
        "status": "reserved",
        "expires_at": future,
        "reservation_key": "resv_rel",
    }
    service._sync_get_order = MagicMock(return_value=order_row)  # type: ignore[method-assign]
    service._sync_release_order = MagicMock(return_value=(100, order_row))  # type: ignore[method-assign]

    fake_redis = AsyncMock()
    fake_redis.delete = AsyncMock()

    with patch("orchestrator.allocator.get_redis", new=AsyncMock(return_value=fake_redis)):
        result = await service.release(order_ref="ord_release1234")

    assert result.success is True
    assert result.released_count == 100
    fake_redis.delete.assert_called_once_with("reservation:ord_release1234")


# === Wave B-4b: get_proxies / extend_order tests ===


async def test_get_proxies_order_not_committed_returns_error() -> None:
    from orchestrator.allocator import AllocatorService
    from orchestrator.schemas import DeliveryFormat

    service = AllocatorService()
    service._sync_get_order = MagicMock(  # type: ignore[method-assign]
        return_value={
            "id": 1,
            "order_ref": "ord_test",
            "status": "reserved",
            "sku_id": 1,
        }
    )
    result = await service.get_proxies(order_ref="ord_test", format=DeliveryFormat.SOCKS5_URI)
    assert result.success is False
    assert result.error == "order_not_committed"


async def test_get_proxies_lazy_creates_delivery_file() -> None:
    from orchestrator.allocator import AllocatorService
    from orchestrator.schemas import DeliveryFormat

    service = AllocatorService()
    service._sync_get_order = MagicMock(  # type: ignore[method-assign]
        return_value={"id": 5, "order_ref": "ord_lazy", "status": "committed", "sku_id": 1}
    )
    service._sync_get_delivery_file = MagicMock(return_value=None)  # type: ignore[method-assign]
    service._sync_list_inventory_for_order = MagicMock(  # type: ignore[method-assign]
        return_value=[
            {
                "id": 1,
                "host": "h.example",
                "port": 1080,
                "login": "u",
                "password": "p",
                "expires_at": None,
                "geo_country": "US",
            }
        ]
    )
    service._sync_upsert_delivery_file = MagicMock(return_value=None)  # type: ignore[method-assign]

    result = await service.get_proxies(order_ref="ord_lazy", format=DeliveryFormat.SOCKS5_URI)

    assert result.success is True
    assert result.line_count == 1
    assert result.content_type == "text/plain"
    assert "socks5://u:p@h.example:1080" in (result.content or "")
    service._sync_upsert_delivery_file.assert_called_once()
    upsert_kwargs = service._sync_upsert_delivery_file.call_args.kwargs
    assert upsert_kwargs["format"] == "socks5_uri"
    assert upsert_kwargs["line_count"] == 1


async def test_get_proxies_format_locked() -> None:
    from orchestrator.allocator import AllocatorService
    from orchestrator.schemas import DeliveryFormat

    service = AllocatorService()
    service._sync_get_order = MagicMock(  # type: ignore[method-assign]
        return_value={"id": 9, "order_ref": "ord_lock", "status": "committed", "sku_id": 1}
    )
    service._sync_get_delivery_file = MagicMock(  # type: ignore[method-assign]
        return_value={
            "order_id": 9,
            "format": "socks5_uri",
            "line_count": 100,
            "content": "socks5://...",
        }
    )

    result = await service.get_proxies(order_ref="ord_lock", format=DeliveryFormat.JSON)
    assert result.success is False
    assert result.error == "format_locked"
    assert result.locked_format == "socks5_uri"


async def test_get_proxies_returns_cached_content() -> None:
    from orchestrator.allocator import AllocatorService
    from orchestrator.schemas import DeliveryFormat

    service = AllocatorService()
    service._sync_get_order = MagicMock(  # type: ignore[method-assign]
        return_value={"id": 11, "order_ref": "ord_cache", "status": "committed", "sku_id": 1}
    )
    service._sync_get_delivery_file = MagicMock(  # type: ignore[method-assign]
        return_value={
            "order_id": 11,
            "format": "socks5_uri",
            "line_count": 42,
            "content": "socks5://u:p@h:1",
        }
    )
    upsert_mock = MagicMock(return_value=None)
    service._sync_upsert_delivery_file = upsert_mock  # type: ignore[method-assign]

    result = await service.get_proxies(order_ref="ord_cache", format=DeliveryFormat.SOCKS5_URI)
    assert result.success is True
    assert result.line_count == 42
    assert result.content == "socks5://u:p@h:1"
    upsert_mock.assert_not_called()


async def test_extend_order_whole_no_filters() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    future = datetime.now(timezone.utc) + timedelta(days=60)
    service._sync_get_order = MagicMock(  # type: ignore[method-assign]
        return_value={"id": 21, "order_ref": "ord_ext", "status": "committed", "sku_id": 1}
    )
    service._sync_extend_inventory = MagicMock(return_value=(150, future))  # type: ignore[method-assign]

    result = await service.extend_order(order_ref="ord_ext", duration_days=30)
    assert result.success is True
    assert result.extended_count == 150
    assert result.new_proxies_expires_at == future
    call_kwargs = service._sync_extend_inventory.call_args.kwargs
    assert call_kwargs["inventory_ids"] is None
    assert call_kwargs["geo_code"] is None


async def test_extend_order_by_geo() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    future = datetime.now(timezone.utc) + timedelta(days=60)
    service._sync_get_order = MagicMock(  # type: ignore[method-assign]
        return_value={"id": 22, "order_ref": "ord_geo", "status": "committed", "sku_id": 1}
    )
    service._sync_extend_inventory = MagicMock(return_value=(40, future))  # type: ignore[method-assign]

    result = await service.extend_order(order_ref="ord_geo", duration_days=30, geo_code="US")
    assert result.success is True
    assert result.extended_count == 40
    call_kwargs = service._sync_extend_inventory.call_args.kwargs
    assert call_kwargs["geo_code"] == "US"
    assert call_kwargs["inventory_ids"] is None


async def test_extend_order_by_ids() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    future = datetime.now(timezone.utc) + timedelta(days=60)
    service._sync_get_order = MagicMock(  # type: ignore[method-assign]
        return_value={"id": 23, "order_ref": "ord_ids", "status": "committed", "sku_id": 1}
    )
    service._sync_extend_inventory = MagicMock(return_value=(3, future))  # type: ignore[method-assign]

    result = await service.extend_order(order_ref="ord_ids", duration_days=15, inventory_ids=[1, 2, 3])
    assert result.success is True
    assert result.extended_count == 3
    call_kwargs = service._sync_extend_inventory.call_args.kwargs
    assert call_kwargs["inventory_ids"] == [1, 2, 3]
    assert call_kwargs["geo_code"] is None


async def test_extend_order_no_inventory_matched() -> None:
    from orchestrator.allocator import AllocatorService

    service = AllocatorService()
    service._sync_get_order = MagicMock(  # type: ignore[method-assign]
        return_value={"id": 24, "order_ref": "ord_nomatch", "status": "committed", "sku_id": 1}
    )
    service._sync_extend_inventory = MagicMock(return_value=(0, None))  # type: ignore[method-assign]

    result = await service.extend_order(order_ref="ord_nomatch", duration_days=30, geo_code="ZZ")
    assert result.success is False
    assert result.error == "no_inventory_matched"
