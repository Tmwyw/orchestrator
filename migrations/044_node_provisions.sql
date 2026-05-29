-- 044_node_provisions.sql — Wave PROVISION-1 ②.
--
-- One row per provision job. provision-prepare (ЭТАП E) inserts it with a
-- one-time secret HASH (sha256) + status='installing'; the node calls
-- /v1/nodes/register with the plaintext secret, the server matches sha256 →
-- finds the job → its account_id → registers the node. partial-failure
-- (install_result.ok=false) flips status to 'failed' so it never hangs.
--
-- Additive + idempotent.

CREATE TABLE IF NOT EXISTS node_provisions (
  job_id             TEXT PRIMARY KEY,
  account_id         BIGINT REFERENCES vultr_accounts(id) ON DELETE SET NULL,
  geo                TEXT,
  region             TEXT,
  plan               TEXT,
  target_stock       INT NOT NULL DEFAULT 4000,
  shared_secret_hash TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'preparing'
                       CHECK (status IN ('preparing','installing','registered','failed','cancelled')),
  ip                 TEXT,
  vultr_instance_id  TEXT,
  install_log_tail   TEXT,
  error              TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at        TIMESTAMPTZ
);

-- /register hot path: match the active job by its secret hash.
CREATE INDEX IF NOT EXISTS idx_node_provisions_secret_active
  ON node_provisions(shared_secret_hash)
  WHERE status = 'installing';

CREATE INDEX IF NOT EXISTS idx_node_provisions_status ON node_provisions(status);
