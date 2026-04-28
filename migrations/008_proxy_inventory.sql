CREATE TABLE IF NOT EXISTS proxy_inventory (
  id                BIGSERIAL PRIMARY KEY,
  sku_id            BIGINT NOT NULL REFERENCES skus(id) ON DELETE CASCADE,
  node_id           TEXT NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
  generation_job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
  login             TEXT NOT NULL,
  password          TEXT NOT NULL,
  host              TEXT NOT NULL,
  port              INT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'pending_validation' CHECK (status IN ('pending_validation','available','reserved','sold','expired_grace','archived','invalid')),
  reservation_key   TEXT,
  reserved_at       TIMESTAMPTZ,
  order_id          BIGINT,
  sold_at           TIMESTAMPTZ,
  expires_at        TIMESTAMPTZ,
  archived_at       TIMESTAMPTZ,
  external_ip       TEXT,
  geo_country       TEXT,
  geo_city          TEXT,
  latency_ms        INT,
  ipv6_only         BOOLEAN,
  dns_sanity        BOOLEAN,
  validation_error  TEXT,
  validated_at      TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inventory_pool ON proxy_inventory(sku_id, node_id, status) WHERE status = 'available';
CREATE INDEX IF NOT EXISTS idx_inventory_pending ON proxy_inventory(sku_id, status) WHERE status = 'pending_validation';
CREATE INDEX IF NOT EXISTS idx_inventory_reserved ON proxy_inventory(reservation_key) WHERE reservation_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_inventory_expires ON proxy_inventory(expires_at) WHERE status IN ('sold','expired_grace');
CREATE INDEX IF NOT EXISTS idx_inventory_order ON proxy_inventory(order_id) WHERE order_id IS NOT NULL;
