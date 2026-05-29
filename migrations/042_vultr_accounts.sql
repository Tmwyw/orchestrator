-- 042_vultr_accounts.sql — Wave PROVISION-1 ②.
--
-- Multiple EQUAL Vultr accounts (no "primary"): each has its own API key + label.
-- A node is driven by the key of ITS account (a Vultr key only sees its own
-- instances). The API key is stored Fernet-encrypted (api_key_enc) — never
-- plaintext. Decryption happens in Python (orchestrator/crypto.py) with
-- ORCH_FERNET_KEY; SQL never sees the plaintext.
--
-- Additive + idempotent.

CREATE TABLE IF NOT EXISTS vultr_accounts (
  id          BIGSERIAL PRIMARY KEY,
  label       TEXT NOT NULL UNIQUE,
  api_key_enc TEXT NOT NULL,
  enabled     BOOLEAN NOT NULL DEFAULT TRUE,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vultr_accounts_enabled ON vultr_accounts(enabled) WHERE enabled = TRUE;
