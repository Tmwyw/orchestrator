"""Static-content sanity for migrations/025_pergb_de_sku_seed.sql.

Tests in this repo don't run against a live DB, so we cannot assert
post-apply rows. Instead we pin the seed file's intent: the agreed
SKU code/geo and the 6-tier price ladder must remain present and
idempotent. If anyone edits the seed by accident this trips.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_SEED_FILE = _MIGRATIONS_DIR / "025_pergb_de_sku_seed.sql"


@pytest.fixture(scope="module")
def seed_sql() -> str:
    return _SEED_FILE.read_text(encoding="utf-8")


def test_seed_file_exists() -> None:
    assert _SEED_FILE.is_file(), f"seed migration missing: {_SEED_FILE}"


def test_seed_inserts_dc_pergb_de_sku(seed_sql: str) -> None:
    # The SKU INSERT must be present, target the right code/kind/geo,
    # and use ON CONFLICT (code) DO NOTHING for idempotency.
    assert "INSERT INTO skus" in seed_sql
    assert "'dc_pergb_de'" in seed_sql
    assert "'datacenter_pergb'" in seed_sql
    assert "'DE'" in seed_sql
    assert "ON CONFLICT (code) DO NOTHING" in seed_sql


def test_seed_inserts_six_tiers_with_agreed_prices(seed_sql: str) -> None:
    # The 6-tier price ladder, in the project-agreed order.
    expected_tiers = [
        ("1::int", "1.20::numeric"),
        ("3::int", "1.10::numeric"),
        ("5::int", "1.00::numeric"),
        ("10::int", "0.95::numeric"),
        ("20::int", "0.85::numeric"),
        ("30::int", "0.80::numeric"),
    ]
    for gb, price in expected_tiers:
        assert gb in seed_sql, f"missing tier gb {gb}"
        assert price in seed_sql, f"missing tier price {price}"

    # Tiers must be inserted into sku_tiers via the SKU lookup,
    # and idempotent on the (sku_id, gb) unique constraint.
    assert "INSERT INTO sku_tiers" in seed_sql
    assert "WHERE s.code = 'dc_pergb_de'" in seed_sql
    assert "ON CONFLICT (sku_id, gb) DO NOTHING" in seed_sql


def test_sku_tiers_table_migration_precedes_seed() -> None:
    # 024 creates sku_tiers; 025 seeds it. Apply order matters.
    assert (_MIGRATIONS_DIR / "024_sku_tiers.sql").is_file()
    assert (_MIGRATIONS_DIR / "025_pergb_de_sku_seed.sql").is_file()
