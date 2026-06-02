-- 049_backfill_binding_target_stock.sql — Wave POOL-PER-NODE.A.
--
-- Stock becomes PER-NODE: refill now keeps each active binding's
-- target_stock on its own node, and a SKU's pool = SUM of its active
-- bindings' targets. Column sku_node_bindings.target_stock was added in
-- migration 045 (DEFAULT 0) but left unused — refill consumed the
-- per-SKU skus.target_stock and split it across bindings by capacity.
--
-- This backfill MUST run BEFORE the new refill logic deploys: every
-- active binding inherits its SKU's current target_stock as its OWN
-- per-node target. Effect:
--   * single-node SKU  → pool = that node's target = old SKU target
--     (unchanged — no surprise generation on deploy).
--   * multi-node  SKU  → pool = N × old SKU target. This is the intended
--     new model (3 nodes each holding the geo target = bigger pool); the
--     operator can tune per-node targets afterwards via PATCH binding.
--
-- Idempotent: only touches active bindings still at the DEFAULT 0, so a
-- re-run (or a binding an operator already set) is never clobbered.

UPDATE sku_node_bindings b
   SET target_stock = COALESCE((SELECT s.target_stock FROM skus s WHERE s.id = b.sku_id), 0),
       updated_at = now()
 WHERE b.is_active = true
   AND b.target_stock = 0;
