ALTER TABLE nodes ADD COLUMN IF NOT EXISTS weight INT NOT NULL DEFAULT 100;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_parallel_jobs INT NOT NULL DEFAULT 1;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_batch_size INT NOT NULL DEFAULT 1500;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS runtime_status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS heartbeat_failures INT NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS generator_script TEXT;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS generator_args_template JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='chk_nodes_runtime_status') THEN
    ALTER TABLE nodes ADD CONSTRAINT chk_nodes_runtime_status CHECK (runtime_status IN ('active','degraded','offline','disabled'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_nodes_runtime_status ON nodes(runtime_status) WHERE runtime_status IN ('active','degraded');
