-- 023_orders_price_compute_trigger.sql
-- Auto-compute orders.price_amount on INSERT based on sku.price_per_piece
-- (or price_per_gb for pergb) * requested_count.
-- Was previously left NULL by allocator, causing bot's transactions
-- check constraint to fail with amount=0.

CREATE OR REPLACE FUNCTION compute_order_price() RETURNS TRIGGER AS $$
BEGIN
  IF NEW.price_amount IS NULL THEN
    SELECT (
      CASE
        WHEN s.product_kind = 'datacenter_pergb' THEN COALESCE(s.price_per_gb, 0)
        ELSE COALESCE(s.price_per_piece, 0)
      END * NEW.requested_count
    )::numeric(18,8)
      INTO NEW.price_amount
      FROM skus s WHERE s.id = NEW.sku_id;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS orders_price_compute ON orders;
CREATE TRIGGER orders_price_compute
  BEFORE INSERT ON orders
  FOR EACH ROW EXECUTE FUNCTION compute_order_price();
