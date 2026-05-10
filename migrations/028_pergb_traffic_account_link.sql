-- 028_pergb_traffic_account_link.sql — link N proxy_inventory rows to one traffic_account
-- Wave PERGB-RFCT-A. Adds reverse FK + per-port counter snapshot. Backfills
-- existing pergb orders (currently 1:1) so their single allocated port points
-- at its traffic_account; ensures aggregate SUM equals current per-port
-- semantics.
--
-- Also relaxes traffic_accounts.inventory_id NOT NULL/UNIQUE constraints —
-- new pergb reserve flow creates the traffic_account *without* allocating any
-- port; the user then calls /v1/pergb/{order_ref}/generate_ports to claim N
-- pool ports lazily. Existing 1:1 rows keep their inventory_id; the column
-- is preserved (not dropped) for backwards-compat with the topup reactivation
-- query that depends on it for legacy clients.

ALTER TABLE proxy_inventory
    ADD COLUMN IF NOT EXISTS traffic_account_id BIGINT
        REFERENCES traffic_accounts(id) ON DELETE SET NULL;

ALTER TABLE proxy_inventory
    ADD COLUMN IF NOT EXISTS bytes_used_snapshot BIGINT NOT NULL DEFAULT 0
        CHECK (bytes_used_snapshot >= 0);

-- Per-port counter anchors: traffic_poll moves the accounting-anchor down
-- to the port row so each port detects its own counter resets. The
-- old account-level anchors (traffic_accounts.last_polled_bytes_in/out)
-- become obsolete once all 1:1 clients are migrated; we keep both for
-- the cutover window.
ALTER TABLE proxy_inventory
    ADD COLUMN IF NOT EXISTS last_polled_bytes_in BIGINT;
ALTER TABLE proxy_inventory
    ADD COLUMN IF NOT EXISTS last_polled_bytes_out BIGINT;

CREATE INDEX IF NOT EXISTS ix_proxy_inventory_traffic_account
    ON proxy_inventory (traffic_account_id)
    WHERE traffic_account_id IS NOT NULL;

-- Backfill: each existing pergb traffic_account currently has 1 allocated port
-- via traffic_accounts.inventory_id. Reverse-link the port row → traffic_account,
-- copy bytes_used + counter anchors so SUM(bytes_used_snapshot) equals the
-- current per-port semantics and counter-reset detection keeps working
-- during the cutover.
UPDATE proxy_inventory pi
   SET traffic_account_id = ta.id,
       bytes_used_snapshot = ta.bytes_used,
       last_polled_bytes_in = ta.last_polled_bytes_in,
       last_polled_bytes_out = ta.last_polled_bytes_out
  FROM traffic_accounts ta
 WHERE pi.id = ta.inventory_id
   AND pi.status = 'allocated_pergb'
   AND pi.traffic_account_id IS NULL;

-- Relax the legacy 1:1 constraints on traffic_accounts.inventory_id. New
-- reserve_pergb writes traffic_accounts without inventory_id; legacy rows
-- keep theirs. UNIQUE drop is safe — no caller relies on it post-refactor.
ALTER TABLE traffic_accounts
    ALTER COLUMN inventory_id DROP NOT NULL;

ALTER TABLE traffic_accounts
    DROP CONSTRAINT IF EXISTS traffic_accounts_inventory_id_key;
