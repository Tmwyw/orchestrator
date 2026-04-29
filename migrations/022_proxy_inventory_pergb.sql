-- migrations/022_proxy_inventory_pergb.sql
-- Wave B-8.1: extend proxy_inventory.status enum with 'allocated_pergb'.
-- Per docs/wave_b8_design.md § 2.1 (D2.1 lock).
--
-- The original CHECK constraint in migration 008 was inline-unnamed; Postgres
-- auto-named it `proxy_inventory_status_check`. DROP IF EXISTS is a safe no-op
-- if the name differs in any environment.

ALTER TABLE proxy_inventory DROP CONSTRAINT IF EXISTS proxy_inventory_status_check;

ALTER TABLE proxy_inventory ADD CONSTRAINT proxy_inventory_status_check
  CHECK (status IN (
    'pending_validation',
    'available',
    'reserved',
    'sold',
    'expired_grace',
    'archived',
    'invalid',
    'allocated_pergb'
  ));
