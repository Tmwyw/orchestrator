"""Tests for B-8.1 pay-per-GB Pydantic schemas + validate_pergb_metadata."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from orchestrator.api_schemas import (
    ReservePergbResponse,
    SkuTier,
    SkuTierTable,
    TopupPergbResponse,
)
from orchestrator.pergb import validate_pergb_metadata


# === SkuTier ===


def test_sku_tier_accepts_decimal_string() -> None:
    tier = SkuTier(gb=5, price_per_gb="9.99")
    assert tier.gb == 5
    assert tier.price_per_gb == Decimal("9.99")
    assert isinstance(tier.price_per_gb, Decimal)


def test_sku_tier_accepts_decimal_object() -> None:
    tier = SkuTier(gb=1, price_per_gb=Decimal("1.50"))
    assert tier.price_per_gb == Decimal("1.50")


def test_sku_tier_rejects_zero_gb() -> None:
    with pytest.raises(ValidationError):
        SkuTier(gb=0, price_per_gb="1.00")


def test_sku_tier_rejects_negative_gb() -> None:
    with pytest.raises(ValidationError):
        SkuTier(gb=-1, price_per_gb="1.00")


def test_sku_tier_rejects_zero_price() -> None:
    with pytest.raises(ValidationError):
        SkuTier(gb=5, price_per_gb="0")


def test_sku_tier_rejects_negative_price() -> None:
    with pytest.raises(ValidationError):
        SkuTier(gb=5, price_per_gb="-1.00")


def test_sku_tier_rejects_garbage_decimal_string() -> None:
    with pytest.raises(ValidationError):
        SkuTier(gb=5, price_per_gb="not-a-number")


def test_sku_tier_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        SkuTier(gb=5, price_per_gb="1.00", surprise="boom")  # type: ignore[call-arg]


# === SkuTierTable ===


def test_sku_tier_table_happy_path() -> None:
    table = SkuTierTable(
        tiers=[
            SkuTier(gb=1, price_per_gb="9.99"),
            SkuTier(gb=3, price_per_gb="8.50"),
            SkuTier(gb=5, price_per_gb="7.25"),
            SkuTier(gb=10, price_per_gb="6.00"),
        ]
    )
    assert len(table.tiers) == 4
    assert [t.gb for t in table.tiers] == [1, 3, 5, 10]


def test_sku_tier_table_rejects_empty_list() -> None:
    with pytest.raises(ValidationError):
        SkuTierTable(tiers=[])


def test_sku_tier_table_rejects_unsorted() -> None:
    with pytest.raises(ValidationError, match="ascending"):
        SkuTierTable(
            tiers=[
                SkuTier(gb=5, price_per_gb="7.25"),
                SkuTier(gb=1, price_per_gb="9.99"),
            ]
        )


def test_sku_tier_table_rejects_duplicate_gb() -> None:
    with pytest.raises(ValidationError, match="ascending"):
        SkuTierTable(
            tiers=[
                SkuTier(gb=1, price_per_gb="9.99"),
                SkuTier(gb=1, price_per_gb="8.99"),
            ]
        )


def test_sku_tier_table_round_trip_from_json_dict() -> None:
    """skus.metadata is loaded from Postgres JSONB as a dict — must validate."""
    payload = {
        "tiers": [
            {"gb": 1, "price_per_gb": "9.99"},
            {"gb": 5, "price_per_gb": "7.50"},
            {"gb": 30, "price_per_gb": "5.00"},
        ]
    }
    table = SkuTierTable.model_validate(payload)
    assert len(table.tiers) == 3
    assert table.tiers[1].price_per_gb == Decimal("7.50")


# === validate_pergb_metadata helper ===


def test_validate_pergb_metadata_happy() -> None:
    table = validate_pergb_metadata(
        {"tiers": [{"gb": 1, "price_per_gb": "9.99"}, {"gb": 3, "price_per_gb": "8.99"}]}
    )
    assert isinstance(table, SkuTierTable)
    assert len(table.tiers) == 2


def test_validate_pergb_metadata_rejects_missing_tiers_key() -> None:
    with pytest.raises(ValidationError):
        validate_pergb_metadata({})


def test_validate_pergb_metadata_rejects_non_dict() -> None:
    with pytest.raises(ValidationError):
        validate_pergb_metadata(["not", "a", "dict"])


def test_validate_pergb_metadata_rejects_unsorted_tiers() -> None:
    with pytest.raises(ValidationError, match="ascending"):
        validate_pergb_metadata(
            {"tiers": [{"gb": 5, "price_per_gb": "7"}, {"gb": 1, "price_per_gb": "9"}]}
        )


# === Decimal-as-string field validators on response models ===


def test_reserve_pergb_response_parses_decimal_string() -> None:
    resp = ReservePergbResponse.model_validate(
        {
            "order_ref": "ord_aaa",
            "expires_at": "2026-06-01T00:00:00Z",
            "port": 32001,
            "host": "1.2.3.4",
            "login": "u",
            "password": "p",
            "bytes_quota": 5_000_000_000,
            "price_amount": "49.95",
        }
    )
    assert resp.success is True
    assert resp.price_amount == Decimal("49.95")


def test_topup_pergb_response_parses_both_decimals() -> None:
    resp = TopupPergbResponse.model_validate(
        {
            "order_ref": "ord_bbb",
            "parent_order_ref": "ord_aaa",
            "topup_sequence": 1,
            "bytes_quota_total": 10_000_000_000,
            "bytes_used": 0,
            "expires_at": "2026-06-01T00:00:00Z",
            "price_amount": "29.99",
            "tier_price_per_gb": "5.99",
        }
    )
    assert resp.price_amount == Decimal("29.99")
    assert resp.tier_price_per_gb == Decimal("5.99")
