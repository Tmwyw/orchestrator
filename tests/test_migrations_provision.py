"""Static-content sanity for the PROVISION-1 ② migrations (042-046).

This repo's tests don't run against a live DB, so we pin each migration's intent
+ idempotency markers. If anyone edits the DDL by accident this trips.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def _read(name: str) -> str:
    return (_MIGRATIONS / name).read_text(encoding="utf-8")


def test_042_vultr_accounts() -> None:
    sql = _read("042_vultr_accounts.sql")
    assert "CREATE TABLE IF NOT EXISTS vultr_accounts" in sql
    assert "label       TEXT NOT NULL UNIQUE" in sql
    assert "api_key_enc TEXT NOT NULL" in sql
    assert "enabled     BOOLEAN NOT NULL DEFAULT TRUE" in sql
    # equal peers: the only PRIMARY is the surrogate id key (no is_primary column)
    assert "is_primary" not in sql.lower()


def test_043_nodes_cols() -> None:
    sql = _read("043_nodes_vultr_cols.sql")
    assert "ADD COLUMN IF NOT EXISTS vultr_account" in sql
    assert "ADD COLUMN IF NOT EXISTS vultr_instance_id TEXT" in sql
    assert "FOREIGN KEY (vultr_account) REFERENCES vultr_accounts(id)" in sql
    assert "ON DELETE SET NULL" in sql


def test_044_node_provisions() -> None:
    sql = _read("044_node_provisions.sql")
    assert "CREATE TABLE IF NOT EXISTS node_provisions" in sql
    assert "job_id             TEXT PRIMARY KEY" in sql
    assert "shared_secret_hash TEXT NOT NULL" in sql
    for st in ("preparing", "installing", "registered", "failed", "cancelled"):
        assert f"'{st}'" in sql
    assert "WHERE status = 'installing'" in sql  # partial index for /register


def test_045_binding_target_stock() -> None:
    sql = _read("045_binding_target_stock.sql")
    assert "ALTER TABLE sku_node_bindings ADD COLUMN IF NOT EXISTS target_stock INT" in sql


def test_046_seed_is_manual_and_idempotent() -> None:
    sql = _read("046_seed_vultr_account_import.sql")
    assert "'imported'" in sql
    assert "__IMPORTED_API_KEY_ENC__" in sql  # operator fills the ciphertext
    assert "ON CONFLICT (label) DO NOTHING" in sql
    assert "vultr_instance_id" in sql


@pytest.mark.parametrize("name", ["042_vultr_accounts.sql", "043_nodes_vultr_cols.sql", "045_binding_target_stock.sql"])
def test_core_migrations_are_idempotent(name: str) -> None:
    sql = _read(name).upper()
    assert "IF NOT EXISTS" in sql
