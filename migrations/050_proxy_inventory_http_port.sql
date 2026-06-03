-- 050_proxy_inventory_http_port.sql — Wave HTTP.B.
--
-- Dual proxies: the node-runtime (HTTP.A) now generates, for every IP, a
-- socks5 listener AND a paired http listener on port-10000. The socks5
-- port stays in proxy_inventory.port (unchanged); this adds the paired
-- http port alongside it on the SAME inventory row, so one IP is still
-- ONE row (the pool does NOT double) and delivery can hand out an
-- http://…:http_port URI next to the socks5://…:port one.
--
-- NULL = legacy socks5-only row (generated before HTTP.A, or by an old
-- node-agent that ignores proxyType): http delivery skips those rows.
-- Set = dual row, http listener live on this port.
--
-- Additive + idempotent. Rollback in migrations/rollback/.

ALTER TABLE proxy_inventory
  ADD COLUMN IF NOT EXISTS http_port INTEGER;

-- A dual proxy can be delivered as an http://… URI. delivery_files.format
-- carries the chosen DeliveryFormat and is pinned by a CHECK (migration
-- 010); widen it to admit 'http_uri', otherwise the upsert in
-- allocator._sync_upsert_delivery_file would be rejected at runtime.
ALTER TABLE delivery_files DROP CONSTRAINT IF EXISTS delivery_files_format_check;
ALTER TABLE delivery_files
  ADD CONSTRAINT delivery_files_format_check
  CHECK (format IN ('socks5_uri','host_port_user_pass','user_pass_at_host_port','json','http_uri'));
