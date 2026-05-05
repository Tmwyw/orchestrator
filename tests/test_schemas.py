"""Tests for orchestrator.schemas — Pydantic v2 DB models."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal


def test_schemas_module_importable() -> None:
    """Module imports cleanly and exposes all expected enums + models."""
    from orchestrator import schemas

    expected_enums = {
        "NodeStatus",
        "NodeRuntimeStatus",
        "JobStatus",
        "JobReason",
        "ProductKind",
        "Protocol",
        "PortAllocationStatus",
        "ProxyInventoryStatus",
        "OrderStatus",
        "DeliveryFormat",
    }
    expected_models = {
        "Node",
        "Job",
        "Sku",
        "SkuNodeBinding",
        "NodePortAllocation",
        "ProxyInventory",
        "Order",
        "DeliveryFile",
    }
    for name in expected_enums | expected_models:
        assert hasattr(schemas, name), f"missing symbol: {name}"


def test_node_runtime_status_values() -> None:
    """NodeRuntimeStatus matches DB CHECK constraint exactly."""
    from orchestrator.schemas import NodeRuntimeStatus

    assert {s.value for s in NodeRuntimeStatus} == {
        "active",
        "degraded",
        "offline",
        "disabled",
    }


def test_proxy_inventory_status_covers_full_lifecycle() -> None:
    """ProxyInventoryStatus has all 8 lifecycle states (B-8.2 added allocated_pergb).

    Mirrors the DB CHECK in migrations/008 + 022.
    """
    from orchestrator.schemas import ProxyInventoryStatus

    assert {s.value for s in ProxyInventoryStatus} == {
        "pending_validation",
        "available",
        "reserved",
        "sold",
        "expired_grace",
        "archived",
        "invalid",
        "allocated_pergb",
    }


def test_traffic_account_status_matches_migration() -> None:
    """TrafficAccountStatus mirrors traffic_accounts.status CHECK (B-8.1 mig 020)."""
    from orchestrator.schemas import TrafficAccountStatus

    assert {s.value for s in TrafficAccountStatus} == {
        "active",
        "depleted",
        "expired",
        "archived",
    }


def test_order_status_values() -> None:
    """OrderStatus matches migration 009 CHECK constraint."""
    from orchestrator.schemas import OrderStatus

    assert {s.value for s in OrderStatus} == {
        "reserved",
        "committed",
        "released",
        "expired",
    }


def test_delivery_format_values() -> None:
    """DeliveryFormat matches migration 010 CHECK constraint."""
    from orchestrator.schemas import DeliveryFormat

    assert {s.value for s in DeliveryFormat} == {
        "socks5_uri",
        "host_port_user_pass",
        "user_pass_at_host_port",
        "json",
    }


def test_node_model_validate_from_dict_row() -> None:
    """Node.model_validate(...) works on a psycopg-shaped dict row."""
    from orchestrator.schemas import Node, NodeRuntimeStatus, NodeStatus

    now = datetime.now(UTC)
    row = {
        "id": "node-1",
        "name": "test-node",
        "url": "https://node-1.example.com",
        "geo": "us-east",
        "status": "ready",
        "capacity": 50,
        "api_key": None,
        "last_health_check": now,
        "weight": 100,
        "max_parallel_jobs": 2,
        "max_batch_size": 1500,
        "runtime_status": "active",
        "heartbeat_failures": 0,
        "last_heartbeat_at": None,
        "generator_script": None,
        "generator_args_template": [],
        "metadata": {},
        "created_at": now,
        "updated_at": now,
    }
    node = Node.model_validate(row)
    assert node.id == "node-1"
    assert node.status is NodeStatus.READY
    assert node.runtime_status is NodeRuntimeStatus.ACTIVE
    assert node.capacity == 50


def test_sku_model_handles_decimal_and_optional_prices() -> None:
    """Sku accepts Decimal prices; both price fields are independently optional."""
    from orchestrator.schemas import ProductKind, Protocol, Sku

    now = datetime.now(UTC)
    row = {
        "id": 1,
        "code": "ipv6-us-30d",
        "product_kind": "ipv6",
        "geo_code": "US",
        "protocol": "socks5",
        "duration_days": 30,
        "price_per_piece": Decimal("0.50"),
        "price_per_gb": None,
        "target_stock": 1000,
        "refill_batch_size": 500,
        "validation_require_ipv6": True,
        "is_active": True,
        "metadata": {},
        "created_at": now,
        "updated_at": now,
    }
    sku = Sku.model_validate(row)
    assert sku.product_kind is ProductKind.IPV6
    assert sku.protocol is Protocol.SOCKS5
    assert sku.price_per_piece == Decimal("0.50")
    assert sku.price_per_gb is None
