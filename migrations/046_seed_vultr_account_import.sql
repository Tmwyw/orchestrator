-- 046_seed_vultr_account_import.sql — Wave PROVISION-1 ② (MANUAL seed).
--
-- ⚠️ NOT auto-applied. Run by the operator AFTER:
--   1. ORCH_FERNET_KEY is set in the orchestrator .env, and
--   2. the current single watchdog Vultr API key has been Fernet-encrypted:
--        python scripts/encrypt_secret.py "$VULTR_API_KEY"
--      (or read it from /opt/netrun-orchestrator/vultr_watchdog.env first)
--   3. the two __PLACEHOLDER__ markers below are filled in.
--
-- This imports the legacy single key as ONE equal vultr_accounts row
-- (label 'imported') and back-fills the existing nodes with that account + each
-- node's Vultr instance id (taken from the hardcoded `declare -A NODES` map in
-- the prod vultr_node_watchdog.sh — the REPORT explains where).
--
-- Idempotent: ON CONFLICT DO NOTHING / guarded UPDATEs.

-- ── 1) import the legacy key as one equal account ─────────────────────────────
-- Replace __IMPORTED_API_KEY_ENC__ with the Fernet ciphertext (NOT the raw key).
INSERT INTO vultr_accounts (label, api_key_enc, enabled)
VALUES ('imported', '__IMPORTED_API_KEY_ENC__', TRUE)
ON CONFLICT (label) DO NOTHING;

-- ── 2) back-fill nodes: vultr_account + vultr_instance_id ──────────────────────
-- Fill the (node_ip, vultr_instance_id) pairs from the prod watchdog's NODES map.
-- One row per existing node (the prod box had ~7). Match is by IP parsed from
-- nodes.url (http://<ip>:8085).
WITH imported AS (
  SELECT id FROM vultr_accounts WHERE label = 'imported'
),
node_iids (node_ip, vultr_instance_id) AS (
  VALUES
    -- __FILL_ME__: ('203.0.113.10', 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'),
    --             ('203.0.113.11', 'ffffffff-1111-2222-3333-444444444444'),
    ('__NODE_IP_1__', '__VULTR_IID_1__')
)
UPDATE nodes n
   SET vultr_account = (SELECT id FROM imported),
       vultr_instance_id = ni.vultr_instance_id,
       updated_at = now()
  FROM node_iids ni
 WHERE n.url LIKE 'http://' || ni.node_ip || ':%'
   AND ni.node_ip NOT LIKE '\_\_%';   -- skip the unfilled placeholder row
