-- migrations/026_traffic_accounts_safety_net.sql
-- Wave D pergb safety net: track whether a depleted account has actually
-- been blocked on the node-agent, so we can retry on transient failures
-- instead of silently delivering unmetered traffic post-quota.
--
-- Columns:
--   node_blocked            — TRUE iff post_disable succeeded against the node.
--                             Cleared back to FALSE when post_enable succeeds
--                             after a top-up reactivation.
--   last_block_attempt_at   — Last time the orchestrator tried post_disable
--                             on this account (success or failure). Throttle
--                             the watchdog retry loop.
--   last_unblock_attempt_at — Mirror for post_enable.
--
-- All three columns are nullable / default-FALSE so existing rows survive
-- the migration without a backfill — the watchdog's first sweep will
-- reconcile state on next cycle.

ALTER TABLE traffic_accounts
  ADD COLUMN IF NOT EXISTS last_block_attempt_at   TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_unblock_attempt_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS node_blocked            BOOLEAN NOT NULL DEFAULT FALSE;

-- Partial index used by the watchdog to find depleted accounts whose
-- node-side block has not been confirmed (initial fail or no attempt yet).
CREATE INDEX IF NOT EXISTS idx_traffic_accounts_block_retry
  ON traffic_accounts (last_block_attempt_at NULLS FIRST)
  WHERE status = 'depleted' AND node_blocked = FALSE;

-- Mirror: active accounts still flagged as blocked on the node — happens
-- briefly between top-up reactivation and the post_enable RTT, or
-- permanently if post_enable failed on retry-needing nodes.
CREATE INDEX IF NOT EXISTS idx_traffic_accounts_unblock_retry
  ON traffic_accounts (last_unblock_attempt_at NULLS FIRST)
  WHERE status = 'active' AND node_blocked = TRUE;
