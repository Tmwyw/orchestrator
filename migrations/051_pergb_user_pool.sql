-- 051_pergb_user_pool.sql — Wave PERGB-POOL-1 Phase A
--
-- Move traffic_accounts from per-ORDER to per-USER (one GB pool per user).
-- Each user's N per-order accounts are MERGED into a single canonical pool
-- (Σ bytes_quota, Σ bytes_used, MAX expires_at); proxy_inventory ports AND
-- traffic_samples are re-pointed to the canonical BEFORE the duplicates are
-- deleted (so neither ports nor accounting history are lost via the
-- ON DELETE CASCADE / SET NULL FKs). Finally enforce one-pool-per-user
-- (UNIQUE user_id) and unhook the per-order coupling (order_id → nullable,
-- drop its UNIQUE + ON DELETE CASCADE FK so deleting one order can no longer
-- wipe the user's whole pool).
--
-- Idempotent: re-running is a near no-op (user_id backfill is WHERE NULL;
-- merges find no non-canonical rows; constraints use IF [NOT] EXISTS).
--
-- ⚠️ Destructive merge (rows deleted). pg_dump the orchestrator DB before
-- applying on prod. Invariant to verify post-apply:
--   * Σ bytes_quota unchanged per user (folded, not lost);
--   * count(proxy_inventory WHERE traffic_account_id IS NOT NULL) unchanged;
--   * exactly one traffic_accounts row per user_id.

-- 1. Per-user key (nullable during backfill).
ALTER TABLE traffic_accounts ADD COLUMN IF NOT EXISTS user_id BIGINT;

-- 2. Backfill from the bound order (order_id is still NOT NULL here, FK-valid).
UPDATE traffic_accounts ta
   SET user_id = o.user_id
  FROM orders o
 WHERE o.id = ta.order_id
   AND ta.user_id IS NULL;

-- 3. Re-point every non-canonical account's PORTS to the canonical (= MIN(id)
--    per user). Must run before the DELETE so ON DELETE SET NULL can't orphan
--    them.
WITH canon AS (
    SELECT id, MIN(id) OVER (PARTITION BY user_id) AS canonical_id
      FROM traffic_accounts
     WHERE user_id IS NOT NULL
)
UPDATE proxy_inventory pi
   SET traffic_account_id = c.canonical_id
  FROM canon c
 WHERE pi.traffic_account_id = c.id
   AND c.id <> c.canonical_id;

-- 4. Re-point accounting history (traffic_samples) to the canonical too —
--    else the ON DELETE CASCADE below would wipe it for merged users.
WITH canon AS (
    SELECT id, MIN(id) OVER (PARTITION BY user_id) AS canonical_id
      FROM traffic_accounts
     WHERE user_id IS NOT NULL
)
UPDATE traffic_samples ts
   SET account_id = c.canonical_id
  FROM canon c
 WHERE ts.account_id = c.id
   AND c.id <> c.canonical_id;

-- 5. Fold Σ quota / Σ used / MAX expiry into the canonical pool + recompute
--    status (active iff quota still ahead of used AND not past expiry; else
--    depleted — the watchdog archives genuinely-dead pools on its next cycle).
WITH agg AS (
    SELECT user_id,
           MIN(id)          AS canonical_id,
           SUM(bytes_quota) AS pool_quota,
           SUM(bytes_used)  AS pool_used,
           MAX(expires_at)  AS pool_expires
      FROM traffic_accounts
     WHERE user_id IS NOT NULL
     GROUP BY user_id
)
UPDATE traffic_accounts ta
   SET bytes_quota = a.pool_quota,
       bytes_used  = a.pool_used,
       expires_at  = a.pool_expires,
       status      = CASE
                       WHEN a.pool_quota > a.pool_used AND a.pool_expires > now()
                       THEN 'active' ELSE 'depleted'
                     END,
       updated_at  = now()
  FROM agg a
 WHERE ta.id = a.canonical_id;

-- 6. Drop the now-empty non-canonical accounts (ports + samples already moved).
WITH canon AS (
    SELECT id, MIN(id) OVER (PARTITION BY user_id) AS canonical_id
      FROM traffic_accounts
     WHERE user_id IS NOT NULL
)
DELETE FROM traffic_accounts ta
 USING canon c
 WHERE ta.id = c.id
   AND c.id <> c.canonical_id;

-- 7. One pool per user.
ALTER TABLE traffic_accounts ALTER COLUMN user_id SET NOT NULL;
ALTER TABLE traffic_accounts
    DROP CONSTRAINT IF EXISTS traffic_accounts_user_id_key;
ALTER TABLE traffic_accounts
    ADD CONSTRAINT traffic_accounts_user_id_key UNIQUE (user_id);
CREATE INDEX IF NOT EXISTS idx_traffic_user ON traffic_accounts(user_id);

-- 8. Unhook per-order coupling: order_id becomes a nullable audit reference
--    (the canonical keeps its first order's id). Drop the UNIQUE + the
--    ON DELETE CASCADE FK so an order deletion can't destroy the pool.
ALTER TABLE traffic_accounts
    DROP CONSTRAINT IF EXISTS traffic_accounts_order_id_key;
ALTER TABLE traffic_accounts
    DROP CONSTRAINT IF EXISTS traffic_accounts_order_id_fkey;
ALTER TABLE traffic_accounts ALTER COLUMN order_id DROP NOT NULL;
