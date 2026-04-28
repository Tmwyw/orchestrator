CREATE TABLE IF NOT EXISTS sku_node_bindings (
  id             BIGSERIAL PRIMARY KEY,
  sku_id         BIGINT NOT NULL REFERENCES skus(id) ON DELETE CASCADE,
  node_id        TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  weight         INT NOT NULL DEFAULT 100,
  max_batch_size INT NOT NULL DEFAULT 1500,
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(sku_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_bindings_sku_active ON sku_node_bindings(sku_id) WHERE is_active = TRUE;
