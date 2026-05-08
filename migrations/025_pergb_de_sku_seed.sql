-- migrations/025_pergb_de_sku_seed.sql
-- Seed for first datacenter_pergb SKU + 6-tier price ladder.
-- Idempotent: ON CONFLICT (code) on skus, ON CONFLICT (sku_id, gb) on sku_tiers.
-- Note: skus has no `name` column (see 005_skus.sql) — display name is
-- resolved from product_kind in /v1/skus/active.

INSERT INTO skus (
  code, product_kind, geo_code, protocol, duration_days,
  price_per_gb, target_stock, refill_batch_size,
  validation_require_ipv6, is_active
)
VALUES (
  'dc_pergb_de', 'datacenter_pergb', 'DE', 'socks5', 30,
  1.00,           -- legacy fallback price_per_gb if no tier matches
  100,            -- target_stock — pergb pool size (one IPv6 alias per active account)
  20,
  TRUE, TRUE
)
ON CONFLICT (code) DO NOTHING;

INSERT INTO sku_tiers (sku_id, gb, price_per_gb)
SELECT s.id, tier.gb, tier.price_per_gb
FROM skus s
CROSS JOIN (VALUES
  (1::int,  1.20::numeric),
  (3::int,  1.10::numeric),
  (5::int,  1.00::numeric),
  (10::int, 0.95::numeric),
  (20::int, 0.85::numeric),
  (30::int, 0.80::numeric)
) AS tier(gb, price_per_gb)
WHERE s.code = 'dc_pergb_de'
ON CONFLICT (sku_id, gb) DO NOTHING;
