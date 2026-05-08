-- migrations/024_sku_tiers.sql
-- Pay-per-GB tier definitions per SKU. One row per (sku_id, gb) pair.
-- Used by /v1/skus/active to return list of tiers for datacenter_pergb SKU.

CREATE TABLE IF NOT EXISTS sku_tiers (
  id            BIGSERIAL PRIMARY KEY,
  sku_id        BIGINT NOT NULL REFERENCES skus(id) ON DELETE CASCADE,
  gb            INT NOT NULL CHECK (gb > 0),
  price_per_gb  NUMERIC(10,2) NOT NULL CHECK (price_per_gb > 0),
  is_active     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (sku_id, gb)
);

CREATE INDEX IF NOT EXISTS idx_sku_tiers_sku_active ON sku_tiers(sku_id) WHERE is_active = TRUE;
