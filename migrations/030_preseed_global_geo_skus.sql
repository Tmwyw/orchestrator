-- 030_preseed_global_geo_skus.sql — pre-create SKUs for all major Vultr DC geos
--
-- Wave UNIVERSAL-NODE. The auto_bind_active_skus=true flag in /v1/nodes/enroll
-- binds a new node to an existing active SKU matching its geo_code. Without
-- pre-seeded SKUs, every new node requires manual `INSERT INTO skus ...` SQL
-- before the binding can succeed.
--
-- We seed all major Vultr regions as is_active=true with target_stock=4000.
-- They have no node_bindings until a node is enrolled, so:
--   - proxy_inventory rows = 0 → stock_available = 0
--   - Bot filters by `stock_available > 0` in the buy flow → SKU stays hidden
--     until a node arrives (no "Germany — 0 шт." visual noise)
-- After bootstrap_new_node enrolls + auto-binds, refill starts filling that
-- geo's pool to 4000 within ~30 minutes. Zero manual SQL per node.
--
-- IDEMPOTENT: re-running is safe (ON CONFLICT DO NOTHING). Existing IN/JP/NL/
-- PL/US rows are preserved untouched.

INSERT INTO skus (
    code,
    product_kind,
    geo_code,
    duration_days,
    price_per_piece,
    target_stock,
    refill_batch_size,
    is_active
) VALUES
    -- North America
    ('ipv6_us', 'ipv6', 'US', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_ca', 'ipv6', 'CA', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_mx', 'ipv6', 'MX', 30, 0.14, 4000, 500, TRUE),
    -- Europe
    ('ipv6_nl', 'ipv6', 'NL', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_pl', 'ipv6', 'PL', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_de', 'ipv6', 'DE', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_fr', 'ipv6', 'FR', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_gb', 'ipv6', 'GB', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_es', 'ipv6', 'ES', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_it', 'ipv6', 'IT', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_se', 'ipv6', 'SE', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_ro', 'ipv6', 'RO', 30, 0.14, 4000, 500, TRUE),
    -- Asia
    ('ipv6_jp', 'ipv6', 'JP', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_in', 'ipv6', 'IN', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_sg', 'ipv6', 'SG', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_kr', 'ipv6', 'KR', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_il', 'ipv6', 'IL', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_ae', 'ipv6', 'AE', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_id', 'ipv6', 'ID', 30, 0.14, 4000, 500, TRUE),
    -- Oceania
    ('ipv6_au', 'ipv6', 'AU', 30, 0.14, 4000, 500, TRUE),
    -- South America
    ('ipv6_br', 'ipv6', 'BR', 30, 0.14, 4000, 500, TRUE),
    ('ipv6_cl', 'ipv6', 'CL', 30, 0.14, 4000, 500, TRUE),
    -- Africa
    ('ipv6_za', 'ipv6', 'ZA', 30, 0.14, 4000, 500, TRUE)
ON CONFLICT (code) DO NOTHING;

-- Sanity readout — count active SKUs by region group after seed.
DO $$
DECLARE
    total INTEGER;
BEGIN
    SELECT COUNT(*) INTO total FROM skus WHERE product_kind = 'ipv6' AND is_active = TRUE;
    RAISE NOTICE 'Total active ipv6 SKUs: %', total;
END $$;
