"""Wave PERGB-POOL-1 Phase A — guard the 051 per-user-pool migration's
critical properties. Like 049, the harness has no live DB (every other test
mocks ``connect()``), so the data behaviour is validated at apply-time; this
text-locks the SQL so a future edit can't silently drop a data-preservation
step or the one-pool-per-user invariant.

A merge is inherently destructive (N rows folded into 1) — there is NO
rollback file by design; pg_dump is the recovery path (noted in the SQL
header + the wave plan).
"""

from __future__ import annotations

from pathlib import Path

_MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
_FWD = _MIGRATIONS / "051_pergb_user_pool.sql"


def test_user_pool_migration_exists_and_merges_per_user() -> None:
    sql = _FWD.read_text(encoding="utf-8").lower()
    # Per-user key added + backfilled from the bound order.
    assert "add column if not exists user_id" in sql
    assert "set user_id = o.user_id" in sql
    # Canonical = MIN(id) per user (deterministic merge target).
    assert "min(id) over (partition by user_id)" in sql
    # Aggregates folded, not lost.
    assert "sum(bytes_quota)" in sql
    assert "sum(bytes_used)" in sql
    assert "max(expires_at)" in sql


def test_user_pool_migration_preserves_ports_and_samples() -> None:
    """Both proxy_inventory ports AND traffic_samples history MUST be
    re-pointed to the canonical BEFORE the non-canonical rows are deleted —
    otherwise the ON DELETE SET NULL / CASCADE FKs would orphan ports or
    wipe accounting history."""
    sql = _FWD.read_text(encoding="utf-8").lower()
    repoint_ports = sql.index("update proxy_inventory")
    repoint_samples = sql.index("update traffic_samples")
    delete_dupes = sql.index("delete from traffic_accounts")
    # Re-points happen strictly before the destructive delete.
    assert repoint_ports < delete_dupes
    assert repoint_samples < delete_dupes


def test_user_pool_migration_enforces_one_pool_and_unhooks_order() -> None:
    sql = _FWD.read_text(encoding="utf-8").lower()
    # One pool per user.
    assert "user_id set not null" in sql
    assert "add constraint traffic_accounts_user_id_key unique (user_id)" in sql
    # Per-order coupling unhooked: order_id loses UNIQUE + the cascade FK and
    # becomes nullable (so deleting one order can't wipe the pool).
    assert "drop constraint if exists traffic_accounts_order_id_key" in sql
    assert "drop constraint if exists traffic_accounts_order_id_fkey" in sql
    assert "alter column order_id drop not null" in sql


def test_user_pool_migration_has_no_rollback_file() -> None:
    # A merge can't be un-merged — recovery is pg_dump, not a down-migration.
    assert (_MIGRATIONS / "rollback" / "051_pergb_user_pool.down.sql").exists() is False
