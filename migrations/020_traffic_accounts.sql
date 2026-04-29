-- migrations/020_traffic_accounts.sql
-- Wave B-8.1: pay-per-GB billing — traffic_accounts table.
-- Per docs/wave_b8_design.md § 2.2 (anchor columns added beyond § 4.3 sketch).

CREATE TABLE traffic_accounts (
  id                     BIGSERIAL PRIMARY KEY,
  order_id               BIGINT NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
  inventory_id           BIGINT NOT NULL UNIQUE REFERENCES proxy_inventory(id) ON DELETE CASCADE,
  bytes_quota            BIGINT NOT NULL CHECK (bytes_quota >= 0),
  bytes_used             BIGINT NOT NULL DEFAULT 0 CHECK (bytes_used >= 0),
  -- Anchor columns for counter-reset detection (D4.4)
  last_polled_bytes_in   BIGINT,
  last_polled_bytes_out  BIGINT,
  last_polled_at         TIMESTAMPTZ,
  status                 TEXT NOT NULL DEFAULT 'active'
                         CHECK (status IN ('active', 'depleted', 'expired', 'archived')),
  depleted_at            TIMESTAMPTZ,
  expires_at             TIMESTAMPTZ NOT NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_traffic_active_polling ON traffic_accounts(last_polled_at)
  WHERE status = 'active';

CREATE INDEX idx_traffic_expiring ON traffic_accounts(expires_at)
  WHERE status = 'active';

CREATE INDEX idx_traffic_inventory ON traffic_accounts(inventory_id);
