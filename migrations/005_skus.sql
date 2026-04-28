CREATE TABLE IF NOT EXISTS skus (
  id              BIGSERIAL PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE,
  product_kind    TEXT NOT NULL CHECK (product_kind IN ('ipv6','datacenter_pergb')),
  geo_code        TEXT NOT NULL,
  protocol        TEXT NOT NULL CHECK (protocol IN ('socks5','http')),
  duration_days   INT NOT NULL DEFAULT 30,
  price_per_piece NUMERIC(10,2),
  price_per_gb    NUMERIC(10,2),
  target_stock    INT NOT NULL DEFAULT 0,
  refill_batch_size INT NOT NULL DEFAULT 500,
  validation_require_ipv6 BOOLEAN NOT NULL DEFAULT TRUE,
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_skus_active_kind ON skus(product_kind, geo_code) WHERE is_active = TRUE;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_jobs_sku') THEN
    ALTER TABLE jobs ADD CONSTRAINT fk_jobs_sku FOREIGN KEY (sku_id) REFERENCES skus(id) ON DELETE SET NULL;
  END IF;
END $$;
