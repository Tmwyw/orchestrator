-- 045_binding_target_stock.sql — Wave PROVISION-1 ②.
--
-- Per-binding target_stock so /register (ЭТАП C step 4) can record the per-node
-- contribution to a geo pool. NOTE: refill.py currently consumes
-- skus.target_stock (per-SKU) and distributes across bindings by capacity —
-- per-binding refill is a follow-up. This column is forward-looking; today
-- /register also sets skus.target_stock so refill actually fills the pool.
--
-- Additive + idempotent.

ALTER TABLE sku_node_bindings ADD COLUMN IF NOT EXISTS target_stock INT NOT NULL DEFAULT 0;
