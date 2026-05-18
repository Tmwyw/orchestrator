-- 040_admin_audit_log.sql — admin write-action audit trail (CATALOG-1 Phase A.2)
--
-- Records every mutating admin action through /v1/admin/* endpoints (SKU
-- create / update / delete, bindings CRUD, tier replace). Read-only
-- endpoints (GET) do NOT write here.
--
-- Schema is intentionally generic so future admin-mutating endpoints
-- (marketplace, mobile, accounts) can reuse the same table without
-- migrations. Per-action detail goes in ``details`` JSONB (e.g. old/new
-- field diffs for PATCH, full body for POST).
--
-- ``actor`` is opaque on purpose — at the orchestrator boundary we only
-- know the api_key principal is "admin". When the bot wraps these calls
-- it can pass an explicit actor via a request header in a later wave.

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id          BIGSERIAL PRIMARY KEY,
    actor       TEXT NOT NULL DEFAULT 'admin',
    action      TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id   TEXT,
    details     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_admin_audit_target
    ON admin_audit_log (target_type, target_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_admin_audit_action_created
    ON admin_audit_log (action, created_at DESC);
