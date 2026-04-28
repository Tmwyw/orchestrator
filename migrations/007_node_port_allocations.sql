CREATE TABLE IF NOT EXISTS node_port_allocations (
  id           BIGSERIAL PRIMARY KEY,
  job_id       TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
  node_id      TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  start_port   INT NOT NULL,
  end_port     INT NOT NULL,
  proxy_count  INT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved','released')),
  released_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_port_alloc_node_status ON node_port_allocations(node_id, status) WHERE status = 'reserved';
