-- ROLLBACK for 049_backfill_binding_target_stock.sql — Wave POOL-PER-NODE.A.
--
-- Lives in migrations/rollback/ (NOT migrations/) on purpose: the runner
-- in orchestrator/migrate.py globs only migrations/*.sql (top level,
-- non-recursive), so this file is never auto-applied as a forward
-- migration. Run it by hand if A must be reverted.
--
-- ⚠️ COARSE / LOSSY: after the backfill there is no flag distinguishing a
-- value the backfill wrote from one an operator later set via PATCH. This
-- resets EVERY active binding back to 0 (the column's default), which is
-- the pre-049 unused state. Pair this with reverting the refill.py
-- deploy — with refill back on skus.target_stock, zeroed per-binding
-- targets are harmless again. Inactive bindings were never touched by 049.

UPDATE sku_node_bindings
   SET target_stock = 0,
       updated_at = now()
 WHERE is_active = true;
