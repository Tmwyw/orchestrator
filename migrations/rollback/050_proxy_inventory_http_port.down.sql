-- ROLLBACK for 050_proxy_inventory_http_port.sql — Wave HTTP.B.
--
-- Lives in migrations/rollback/ (NOT migrations/) so the migrate.py
-- runner — which globs only migrations/*.sql, non-recursive — never
-- auto-applies it as a forward migration. Run by hand to revert.
--
-- Drops the paired-http-port column. Pair with reverting the HTTP.B
-- code deploy (delivery/ingest stop reading http_port). socks5 ports
-- (proxy_inventory.port) are untouched — the socks pool is unaffected.

-- Restore the migration-010 CHECK (without 'http_uri'). Any persisted
-- http_uri delivery_files rows must be cleared first or this ADD fails.
ALTER TABLE delivery_files DROP CONSTRAINT IF EXISTS delivery_files_format_check;
ALTER TABLE delivery_files
  ADD CONSTRAINT delivery_files_format_check
  CHECK (format IN ('socks5_uri','host_port_user_pass','user_pass_at_host_port','json'));

ALTER TABLE proxy_inventory
  DROP COLUMN IF EXISTS http_port;
