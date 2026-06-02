-- 048_nodes_egress_watchdog.sql — Wave WATCHDOG-EGRESS-CHECK.
--
-- Per-node OUTBOUND (egress) health state for the egress watchdog. The
-- existing health (/v1/nodes/health) only pings the node-agent on :8085
-- (inbound reachability) and is blind to the recurrent Vultr abuse-block
-- failure mode: the agent stays reachable while outbound proxy traffic
-- ("Can't complete SOCKS5 connection") is dead. The egress watchdog probes
-- each node's actual outbound internet through one of its own proxies and
-- reboots the node when egress stays dead.
--
-- egress_fail_streak / egress_last_reboot_at are the watchdog's own state
-- kept IN the DB so a service restart never loses the consecutive-fail
-- count or the reboot cooldown. Additive + idempotent.
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS egress_ok BOOLEAN;                 -- NULL = not checked yet
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS egress_checked_at TIMESTAMPTZ;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS egress_fail_streak INT NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS egress_last_reboot_at TIMESTAMPTZ;
