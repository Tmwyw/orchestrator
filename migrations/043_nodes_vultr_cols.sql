-- 043_nodes_vultr_cols.sql — Wave PROVISION-1 ②.
--
-- Tie each node to the Vultr account that owns it + its Vultr instance id, so
-- the watchdog (and reboot endpoint) can reach the right account's API key.
-- vultr_account is nullable: hand-enrolled / legacy nodes may have none.
-- vultr_instance_id is an explicit indexable column (chosen over nodes.metadata
-- jsonb — DUALSTACK_PLAN open-Q #11 — for direct watchdog joins).
--
-- Additive + idempotent.

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS vultr_account     BIGINT;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS vultr_instance_id TEXT;

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_nodes_vultr_account') THEN
    ALTER TABLE nodes
      ADD CONSTRAINT fk_nodes_vultr_account
      FOREIGN KEY (vultr_account) REFERENCES vultr_accounts(id) ON DELETE SET NULL;
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_nodes_vultr_account ON nodes(vultr_account) WHERE vultr_account IS NOT NULL;
