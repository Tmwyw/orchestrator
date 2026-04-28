CREATE TABLE IF NOT EXISTS delivery_files (
  id                BIGSERIAL PRIMARY KEY,
  order_id          BIGINT NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
  format            TEXT NOT NULL CHECK (format IN ('socks5_uri','host_port_user_pass','user_pass_at_host_port','json')),
  line_count        INT NOT NULL,
  checksum_sha256   TEXT NOT NULL,
  content           TEXT,
  content_expires_at TIMESTAMPTZ NOT NULL,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_delivery_content_expires ON delivery_files(content_expires_at) WHERE content IS NOT NULL;
