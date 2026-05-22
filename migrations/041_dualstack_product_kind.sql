-- Extend skus.product_kind CHECK to allow 'dualstack' (O1+O2 dualstack wave).
-- Idempotent: drop the existing CHECK (regardless of its auto-generated name)
-- and re-add with the broader set. Existing rows ('ipv6','datacenter_pergb')
-- remain valid under the new constraint. The partial index
-- idx_skus_active_kind is column-based, NOT constraint-based — unaffected.

DO $$
DECLARE
  conname TEXT;
BEGIN
  SELECT c.conname INTO conname
  FROM pg_constraint c
  JOIN pg_class t ON t.oid = c.conrelid
  WHERE t.relname = 'skus'
    AND c.contype = 'c'
    AND pg_get_constraintdef(c.oid) ILIKE '%product_kind%';

  IF conname IS NOT NULL THEN
    EXECUTE format('ALTER TABLE skus DROP CONSTRAINT %I', conname);
  END IF;

  ALTER TABLE skus
    ADD CONSTRAINT skus_product_kind_check
    CHECK (product_kind IN ('ipv6','datacenter_pergb','dualstack'));
END $$;
