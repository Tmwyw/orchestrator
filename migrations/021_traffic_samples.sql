-- migrations/021_traffic_samples.sql
-- Wave B-8.1: per-cycle counter samples with delta + reset-detection flag.
-- Per docs/wave_b8_design.md § 2.3.

CREATE TABLE traffic_samples (
  id                      BIGSERIAL PRIMARY KEY,
  account_id              BIGINT NOT NULL REFERENCES traffic_accounts(id) ON DELETE CASCADE,
  bytes_in                BIGINT NOT NULL CHECK (bytes_in >= 0),
  bytes_out               BIGINT NOT NULL CHECK (bytes_out >= 0),
  bytes_in_delta          BIGINT NOT NULL,
  bytes_out_delta         BIGINT NOT NULL,
  counter_reset_detected  BOOLEAN NOT NULL DEFAULT FALSE,
  collected_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_samples_account ON traffic_samples(account_id, collected_at DESC);
