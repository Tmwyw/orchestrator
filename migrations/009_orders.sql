CREATE TABLE IF NOT EXISTS orders (
  id                BIGSERIAL PRIMARY KEY,
  order_ref         TEXT NOT NULL UNIQUE,
  user_id           BIGINT NOT NULL,
  sku_id            BIGINT NOT NULL REFERENCES skus(id),
  status            TEXT NOT NULL CHECK (status IN ('reserved','committed','released','expired')),
  requested_count   INT NOT NULL,
  allocated_count   INT NOT NULL DEFAULT 0,
  reservation_key   TEXT NOT NULL,
  reserved_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at        TIMESTAMPTZ NOT NULL,
  committed_at      TIMESTAMPTZ,
  released_at       TIMESTAMPTZ,
  proxies_expires_at TIMESTAMPTZ,
  price_amount      NUMERIC(18,8),
  idempotency_key   TEXT UNIQUE,
  metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_reserved_expires ON orders(expires_at) WHERE status = 'reserved';
CREATE INDEX IF NOT EXISTS idx_orders_committed_expiring ON orders(proxies_expires_at) WHERE status = 'committed';

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_inventory_order') THEN
    ALTER TABLE proxy_inventory ADD CONSTRAINT fk_inventory_order FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL;
  END IF;
END $$;
