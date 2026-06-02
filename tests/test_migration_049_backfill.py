"""Wave POOL-PER-NODE.A — guard the 049 backfill migration's critical
properties. The test harness has no live DB (every other test mocks
``connect()``), so the data behaviour itself is validated at apply-time;
this locks the SQL so a future edit can't silently drop the idempotency
guard or widen the scope to inactive / already-set bindings.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_FWD = _MIGRATIONS / "049_backfill_binding_target_stock.sql"
_ROLLBACK = _MIGRATIONS / "rollback" / "049_backfill_binding_target_stock.down.sql"


def test_backfill_migration_exists_and_is_scoped() -> None:
    sql = _FWD.read_text(encoding="utf-8").lower()
    assert "update sku_node_bindings" in sql
    # Seeds each active binding from its SKU's current per-SKU target.
    assert "target_stock = coalesce(" in sql
    assert "from skus s where s.id = b.sku_id" in sql
    # Active-only: inactive bindings keep the default 0.
    assert "is_active = true" in sql
    # Idempotency guard: only rows still at DEFAULT 0 are touched, so a
    # re-run never clobbers an operator-set per-node target.
    assert "b.target_stock = 0" in sql


def test_backfill_rollback_exists_outside_glob() -> None:
    # The rollback MUST live in migrations/rollback/ (not migrations/) or
    # migrate.py's glob("*.sql") would auto-apply it as a forward step.
    assert _ROLLBACK.exists()
    assert (_MIGRATIONS / "049_backfill_binding_target_stock.down.sql").exists() is False
    rb = _ROLLBACK.read_text(encoding="utf-8").lower()
    assert "update sku_node_bindings" in rb
    assert "target_stock = 0" in rb
