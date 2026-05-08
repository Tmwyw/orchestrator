"""Service-level tests for PergbService (Wave B-8.2 internals).

Mocks ``connect()`` + ``get_redis()`` + node_client to exercise the
DB-shape, Redis idempotency, and reactivation post_enable paths
without a live database.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import psycopg

from orchestrator.pergb_service import (
    PergbService,
)


def _run(coro):
    return asyncio.run(coro)


def _make_cursor(
    *,
    fetchall_queue: list[list[dict[str, Any]]] | None = None,
    fetchone_queue: list[dict[str, Any] | None] | None = None,
    execute_side_effect: Any = None,
) -> MagicMock:
    cursor = MagicMock(name="cursor")
    fa_queue = list(fetchall_queue or [])
    fo_queue = list(fetchone_queue or [])
    cursor.execute = MagicMock(side_effect=execute_side_effect)
    cursor.fetchall = MagicMock(side_effect=lambda: fa_queue.pop(0) if fa_queue else [])
    cursor.fetchone = MagicMock(side_effect=lambda: fo_queue.pop(0) if fo_queue else None)
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=None)
    return cursor


def _make_conn(cursor: MagicMock) -> MagicMock:
    conn = MagicMock(name="conn")
    conn.cursor = MagicMock(return_value=cursor)
    conn.rollback = MagicMock()
    return conn


def _make_phased_connect(*phases: MagicMock):
    iterator = iter(list(phases))

    @contextmanager
    def fake_connect():
        yield next(iterator)

    return fake_connect


def _make_redis_mock(cached: str | None = None) -> AsyncMock:
    """Returns an awaitable mock with .get / .set / .setex stubs."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=cached)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    return redis


_PERGB_SKU_METADATA = {
    "tiers": [
        {"gb": 1, "price_per_gb": "1.20"},
        {"gb": 5, "price_per_gb": "1.00"},
        {"gb": 10, "price_per_gb": "0.95"},
    ]
}


# ===== reserve_pergb =====


def test_reserve_pergb_happy_path() -> None:
    sku_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 5,
                "code": "sku_pergb_us",
                "product_kind": "datacenter_pergb",
                "metadata": _PERGB_SKU_METADATA,
                "duration_days": 30,
                "is_active": True,
            }
        ],
    )
    create_cursor = _make_cursor(
        fetchone_queue=[
            # 1. Inventory claim returning row
            {
                "id": 100,
                "node_id": "node-x",
                "port": 32001,
                "host": "2001:db8::1",
                "login": "u",
                "password": "p",
            },
            # 2. Order INSERT RETURNING id
            {"id": 999},
        ],
    )
    fake_connect = _make_phased_connect(_make_conn(sku_cursor), _make_conn(create_cursor))
    redis = _make_redis_mock()

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=redis)),
    ):
        result = _run(
            PergbService().reserve_pergb(
                user_id=1,
                sku_id=5,
                gb_amount=10,
                idempotency_key="K1",
            )
        )

    assert result.success is True
    assert result.port == 32001
    assert result.bytes_quota == 10 * 1024 * 1024 * 1024
    assert result.price_amount == Decimal("9.50000000")
    # Idempotency cache write
    redis.set.assert_awaited()


def test_reserve_pergb_idem_cache_hit_skips_db() -> None:
    """Cached idempotency response → no DB connect at all."""
    cached_payload = (
        '{"success": true, "order_ref": "ord_cached", '
        '"expires_at": "2026-06-01T00:00:00+00:00", '
        '"port": 32001, "host": "h", "login": "u", "password": "p", '
        '"bytes_quota": 1024, "price_amount": "1.00"}'
    )
    redis = _make_redis_mock(cached=cached_payload)
    connect_calls: list[str] = []

    @contextmanager
    def fake_connect():
        connect_calls.append("called")
        yield MagicMock()

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=redis)),
    ):
        result = _run(
            PergbService().reserve_pergb(
                user_id=1,
                sku_id=5,
                gb_amount=1,
                idempotency_key="hit",
            )
        )

    assert result.success is True
    assert result.order_ref == "ord_cached"
    assert connect_calls == []


def test_reserve_pergb_sku_not_found_returns_error() -> None:
    sku_cursor = _make_cursor(fetchone_queue=[None])
    fake_connect = _make_phased_connect(_make_conn(sku_cursor))
    redis = _make_redis_mock()

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=redis)),
    ):
        result = _run(PergbService().reserve_pergb(user_id=1, sku_id=999, gb_amount=10))

    assert result.success is False
    assert result.error == "sku_not_found"


def test_reserve_pergb_sku_not_pergb_returns_error() -> None:
    sku_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 5,
                "product_kind": "ipv6",
                "metadata": {},
                "duration_days": 30,
                "is_active": True,
            }
        ]
    )
    fake_connect = _make_phased_connect(_make_conn(sku_cursor))

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=_make_redis_mock())),
    ):
        result = _run(PergbService().reserve_pergb(user_id=1, sku_id=5, gb_amount=10))

    assert result.error == "sku_not_pergb"


def test_reserve_pergb_invalid_tier_returns_available_list() -> None:
    sku_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 5,
                "product_kind": "datacenter_pergb",
                "metadata": _PERGB_SKU_METADATA,
                "duration_days": 30,
                "is_active": True,
            }
        ]
    )
    fake_connect = _make_phased_connect(_make_conn(sku_cursor))

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=_make_redis_mock())),
    ):
        result = _run(PergbService().reserve_pergb(user_id=1, sku_id=5, gb_amount=7))

    assert result.error == "invalid_tier_amount"
    assert result.available_tiers == [1, 5, 10]


def test_reserve_pergb_insufficient_inventory_returns_error() -> None:
    sku_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 5,
                "product_kind": "datacenter_pergb",
                "metadata": _PERGB_SKU_METADATA,
                "duration_days": 30,
                "is_active": True,
            }
        ]
    )
    create_cursor = _make_cursor(fetchone_queue=[None])  # inventory claim returns no row
    fake_connect = _make_phased_connect(_make_conn(sku_cursor), _make_conn(create_cursor))

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=_make_redis_mock())),
    ):
        result = _run(PergbService().reserve_pergb(user_id=1, sku_id=5, gb_amount=10))

    assert result.error == "insufficient_inventory"


# ===== topup_pergb =====


def test_topup_pergb_happy_path() -> None:
    parent_cursor = _make_cursor(
        fetchone_queue=[
            {
                "order_id": 999,
                "order_ref": "ord_abc",
                "user_id": 1,
                "sku_id": 5,
                "account_id": 7,
                "account_status": "active",
                "bytes_quota": 1_000_000_000,
                "bytes_used": 100_000_000,
                "expires_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "node_id": "node-x",
                "port": 32001,
                "node_url": "http://node-x:8085",
                "node_api_key": "k1",
            }
        ]
    )
    sku_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 5,
                "product_kind": "datacenter_pergb",
                "metadata": _PERGB_SKU_METADATA,
                "duration_days": 30,
                "is_active": True,
            }
        ]
    )
    apply_cursor = _make_cursor(
        fetchone_queue=[
            {"c": 0},  # topup count
            {"id": 1234},  # new order INSERT RETURNING id
            {
                "bytes_quota": 11_000_000_000,
                "bytes_used": 100_000_000,
                "expires_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "status": "active",
                "just_reactivated": False,
            },
        ]
    )
    fake_connect = _make_phased_connect(
        _make_conn(parent_cursor),
        _make_conn(sku_cursor),
        _make_conn(apply_cursor),
    )
    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=_make_redis_mock())),
    ):
        result = _run(
            PergbService().topup_pergb(
                parent_order_ref="ord_abc",
                sku_id=5,
                gb_amount=10,
            )
        )

    assert result.success is True
    assert result.parent_order_ref == "ord_abc"
    assert result.topup_sequence == 1
    assert result.bytes_quota_total == 11_000_000_000
    assert result.reactivated is False


def test_topup_pergb_sku_mismatch() -> None:
    parent_cursor = _make_cursor(
        fetchone_queue=[
            {
                "order_id": 999,
                "order_ref": "ord_abc",
                "user_id": 1,
                "sku_id": 5,
                "account_id": 7,
                "account_status": "active",
                "bytes_quota": 1_000_000_000,
                "bytes_used": 0,
                "expires_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "node_id": "node-x",
                "port": 32001,
                "node_url": "http://node-x:8085",
                "node_api_key": None,
            }
        ]
    )
    fake_connect = _make_phased_connect(_make_conn(parent_cursor))

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=_make_redis_mock())),
    ):
        result = _run(PergbService().topup_pergb(parent_order_ref="ord_abc", sku_id=99, gb_amount=10))

    assert result.error == "sku_mismatch_for_topup"


def test_topup_pergb_account_not_renewable_when_expired() -> None:
    parent_cursor = _make_cursor(
        fetchone_queue=[
            {
                "order_id": 999,
                "order_ref": "ord_abc",
                "user_id": 1,
                "sku_id": 5,
                "account_id": 7,
                "account_status": "expired",
                "bytes_quota": 1_000_000_000,
                "bytes_used": 1_000_000_000,
                "expires_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "node_id": "node-x",
                "port": 32001,
                "node_url": "http://node-x:8085",
                "node_api_key": None,
            }
        ]
    )
    sku_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 5,
                "product_kind": "datacenter_pergb",
                "metadata": _PERGB_SKU_METADATA,
                "duration_days": 30,
                "is_active": True,
            }
        ]
    )
    fake_connect = _make_phased_connect(
        _make_conn(parent_cursor),
        _make_conn(sku_cursor),
    )
    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=_make_redis_mock())),
    ):
        result = _run(PergbService().topup_pergb(parent_order_ref="ord_abc", sku_id=5, gb_amount=10))

    assert result.error == "account_not_renewable"
    assert result.current_status == "expired"


def test_topup_pergb_reactivation_calls_post_enable() -> None:
    """When account flips depleted → active, post_enable fires on the node."""
    parent_cursor = _make_cursor(
        fetchone_queue=[
            {
                "order_id": 999,
                "order_ref": "ord_abc",
                "user_id": 1,
                "sku_id": 5,
                "account_id": 7,
                "account_status": "depleted",
                "bytes_quota": 1_000_000_000,
                "bytes_used": 1_000_000_000,
                "expires_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "node_id": "node-x",
                "port": 32001,
                "node_url": "http://node-x:8085",
                "node_api_key": "k1",
            }
        ]
    )
    sku_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 5,
                "product_kind": "datacenter_pergb",
                "metadata": _PERGB_SKU_METADATA,
                "duration_days": 30,
                "is_active": True,
            }
        ]
    )
    apply_cursor = _make_cursor(
        fetchone_queue=[
            {"c": 0},
            {"id": 1234},
            {
                "bytes_quota": 11_000_000_000,
                "bytes_used": 1_000_000_000,
                "expires_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "status": "active",
                "just_reactivated": True,
            },
        ]
    )
    # Fourth phase: _best_effort_post_enable now records the unblock attempt
    # (node_blocked=FALSE on success, last_unblock_attempt_at always stamped).
    unblock_record = _make_cursor()
    fake_connect = _make_phased_connect(
        _make_conn(parent_cursor),
        _make_conn(sku_cursor),
        _make_conn(apply_cursor),
        _make_conn(unblock_record),
    )

    enable_calls: list[tuple[Any, ...]] = []

    def fake_post_enable(url, api_key, port, timeout_sec=10):
        enable_calls.append((url, api_key, port))
        return {"action": "started"}

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=_make_redis_mock())),
        patch("orchestrator.pergb_service.node_client.post_enable", side_effect=fake_post_enable),
    ):
        result = _run(PergbService().topup_pergb(parent_order_ref="ord_abc", sku_id=5, gb_amount=10))

    assert result.success is True
    assert result.reactivated is True
    assert enable_calls == [("http://node-x:8085", "k1", 32001)]

    # Success path: node_blocked=FALSE + last_unblock_attempt_at stamped.
    record_calls = [c.args[0] for c in unblock_record.execute.call_args_list]
    assert any("node_blocked = FALSE" in s and "last_unblock_attempt_at = now()" in s for s in record_calls)


def test_topup_pergb_reactivation_post_enable_failure_keeps_node_blocked() -> None:
    """post_enable failing on reactivation: last_unblock_attempt_at stamped
    so the watchdog can throttle, but node_blocked stays TRUE so the
    watchdog will retry."""
    from orchestrator.node_client import NodeAgentError

    parent_cursor = _make_cursor(
        fetchone_queue=[
            {
                "order_id": 999,
                "order_ref": "ord_abc",
                "user_id": 1,
                "sku_id": 5,
                "account_id": 7,
                "account_status": "depleted",
                "bytes_quota": 1_000_000_000,
                "bytes_used": 1_000_000_000,
                "expires_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "node_id": "node-x",
                "port": 32001,
                "node_url": "http://node-x:8085",
                "node_api_key": "k1",
            }
        ]
    )
    sku_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 5,
                "product_kind": "datacenter_pergb",
                "metadata": _PERGB_SKU_METADATA,
                "duration_days": 30,
                "is_active": True,
            }
        ]
    )
    apply_cursor = _make_cursor(
        fetchone_queue=[
            {"c": 0},
            {"id": 1234},
            {
                "bytes_quota": 11_000_000_000,
                "bytes_used": 1_000_000_000,
                "expires_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "status": "active",
                "just_reactivated": True,
            },
        ]
    )
    unblock_record = _make_cursor()
    fake_connect = _make_phased_connect(
        _make_conn(parent_cursor),
        _make_conn(sku_cursor),
        _make_conn(apply_cursor),
        _make_conn(unblock_record),
    )

    def boom_post_enable(url, api_key, port, timeout_sec=10):
        raise NodeAgentError("enable_failed", status_code=502)

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=_make_redis_mock())),
        patch("orchestrator.pergb_service.node_client.post_enable", side_effect=boom_post_enable),
    ):
        result = _run(PergbService().topup_pergb(parent_order_ref="ord_abc", sku_id=5, gb_amount=10))

    # Top-up itself still succeeds — quota grew, account flipped to active.
    assert result.success is True
    assert result.reactivated is True

    record_calls = [c.args[0] for c in unblock_record.execute.call_args_list]
    # last_unblock_attempt_at stamped (so watchdog throttles); node_blocked NOT
    # cleared (so watchdog will retry post_enable).
    assert any("last_unblock_attempt_at = now()" in s for s in record_calls)
    assert not any("node_blocked = FALSE" in s for s in record_calls)


def test_topup_pergb_idempotency_unique_violation_path_b() -> None:
    """Concurrent same-key top-up: second INSERT raises UniqueViolation; we
    fetch the existing row and return its response (D6.4 Path B)."""
    parent_cursor = _make_cursor(
        fetchone_queue=[
            {
                "order_id": 999,
                "order_ref": "ord_abc",
                "user_id": 1,
                "sku_id": 5,
                "account_id": 7,
                "account_status": "active",
                "bytes_quota": 1_000_000_000,
                "bytes_used": 0,
                "expires_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "node_id": "node-x",
                "port": 32001,
                "node_url": "http://node-x:8085",
                "node_api_key": None,
            }
        ]
    )
    sku_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 5,
                "product_kind": "datacenter_pergb",
                "metadata": _PERGB_SKU_METADATA,
                "duration_days": 30,
                "is_active": True,
            }
        ]
    )

    # Simulate UniqueViolation on the orders INSERT (the 2nd execute call —
    # 1st is the topup count SELECT). Build an execute side-effect:
    execute_calls = {"n": 0}

    def execute_side_effect(*args, **kwargs):
        execute_calls["n"] += 1
        if execute_calls["n"] == 2:
            raise psycopg.errors.UniqueViolation("dup")
        return

    apply_cursor = _make_cursor(
        fetchone_queue=[
            {"c": 0},  # topup count SELECT
        ],
        execute_side_effect=execute_side_effect,
    )
    # The fetch_topup_by_idem call opens a fresh connect()
    fetch_existing = _make_cursor(
        fetchone_queue=[
            {
                "order_ref": "ord_topup_existing",
                "metadata": {
                    "parent_order_ref": "ord_abc",
                    "topup_sequence": 1,
                    "tier_price_per_gb": "0.95",
                },
                "price_amount": "9.50",
                "proxies_expires_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "bytes_quota": 11_000_000_000,
                "bytes_used": 0,
            }
        ]
    )

    fake_connect = _make_phased_connect(
        _make_conn(parent_cursor),
        _make_conn(sku_cursor),
        _make_conn(apply_cursor),
        _make_conn(fetch_existing),
    )

    with (
        patch("orchestrator.pergb_service.connect", new=fake_connect),
        patch("orchestrator.pergb_service.get_redis", new=AsyncMock(return_value=_make_redis_mock())),
    ):
        result = _run(
            PergbService().topup_pergb(
                parent_order_ref="ord_abc",
                sku_id=5,
                gb_amount=10,
                idempotency_key="K_dup",
            )
        )

    assert result.success is True
    assert result.order_ref == "ord_topup_existing"
    assert result.topup_sequence == 1


# ===== get_traffic =====


def test_get_traffic_happy_path() -> None:
    snapshot_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 999,
                "metadata": {},
                "account_id": 7,
                "status": "active",
                "bytes_quota": 10 * 1024 * 1024 * 1024,
                "bytes_used": 5 * 1024 * 1024 * 1024,
                "last_polled_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "expires_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "depleted_at": None,
                "node_id": "node-x",
                "port": 32001,
            }
        ]
    )
    fake_connect = _make_phased_connect(_make_conn(snapshot_cursor))

    with patch("orchestrator.pergb_service.connect", new=fake_connect):
        result = _run(PergbService().get_traffic(parent_order_ref="ord_abc"))

    assert result.success is True
    assert result.usage_pct == 0.5
    assert result.bytes_remaining == 5 * 1024 * 1024 * 1024
    assert result.over_usage_bytes == 0


def test_get_traffic_top_up_order_returns_helpful_404() -> None:
    """Top-up order (has parent_order_ref in metadata, no traffic_account row)
    → traffic_account_not_found with helpful detail."""
    snapshot_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 999,
                "metadata": {"parent_order_ref": "ord_parent"},
                "account_id": None,
                "status": None,
                "bytes_quota": None,
                "bytes_used": None,
                "last_polled_at": None,
                "expires_at": None,
                "depleted_at": None,
                "node_id": None,
                "port": None,
            }
        ]
    )
    fake_connect = _make_phased_connect(_make_conn(snapshot_cursor))

    with patch("orchestrator.pergb_service.connect", new=fake_connect):
        result = _run(PergbService().get_traffic(parent_order_ref="ord_topup"))

    assert result.error == "traffic_account_not_found"
    assert "parent" in (result.detail or "")


def test_get_traffic_order_not_found() -> None:
    snapshot_cursor = _make_cursor(fetchone_queue=[None])
    fake_connect = _make_phased_connect(_make_conn(snapshot_cursor))

    with patch("orchestrator.pergb_service.connect", new=fake_connect):
        result = _run(PergbService().get_traffic(parent_order_ref="ord_missing"))

    assert result.error == "order_not_found"


def test_get_traffic_over_usage_caps_pct_at_one() -> None:
    """bytes_used > bytes_quota → usage_pct=1.0, over_usage_bytes positive."""
    snapshot_cursor = _make_cursor(
        fetchone_queue=[
            {
                "id": 999,
                "metadata": {},
                "account_id": 7,
                "status": "depleted",
                "bytes_quota": 1000,
                "bytes_used": 1100,
                "last_polled_at": None,
                "expires_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
                "depleted_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "node_id": "node-x",
                "port": 32001,
            }
        ]
    )
    fake_connect = _make_phased_connect(_make_conn(snapshot_cursor))

    with patch("orchestrator.pergb_service.connect", new=fake_connect):
        result = _run(PergbService().get_traffic(parent_order_ref="ord_abc"))

    assert result.success is True
    assert result.usage_pct == 1.0
    assert result.over_usage_bytes == 100
    assert result.bytes_remaining == 0
