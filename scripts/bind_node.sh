#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/bind_node.sh <sku_code> <node_id> [weight] [max_batch_size]

Defaults: weight=100, max_batch_size=1500

Example:
  bash scripts/bind_node.sh ipv6_us_socks5 node-de-1
  bash scripts/bind_node.sh ipv6_us_socks5 node-de-1 100 50
EOF
  exit 1
}

[ $# -ge 2 ] || usage
SKU_CODE="$1"
NODE_ID="$2"
WEIGHT="${3:-100}"
MAX_BATCH="${4:-1500}"

APP_HOME="${APP_HOME:-/opt/netrun-orchestrator}"
if [ -f "$APP_HOME/.env" ] && [ -z "${DATABASE_URL:-}" ]; then
  set -a; . "$APP_HOME/.env"; set +a
fi
[ -n "${DATABASE_URL:-}" ] || { echo "ERROR: DATABASE_URL not set" >&2; exit 2; }

psql "$DATABASE_URL" <<SQL
WITH s AS (SELECT id FROM skus WHERE code = '${SKU_CODE}'),
     n AS (SELECT id FROM nodes WHERE id = '${NODE_ID}')
INSERT INTO sku_node_bindings (sku_id, node_id, weight, max_batch_size, is_active)
SELECT s.id, n.id, ${WEIGHT}, ${MAX_BATCH}, TRUE
FROM s, n
ON CONFLICT (sku_id, node_id) DO UPDATE SET
  weight = EXCLUDED.weight,
  max_batch_size = EXCLUDED.max_batch_size,
  is_active = TRUE,
  updated_at = NOW()
RETURNING sku_id, node_id, weight, max_batch_size;
SQL
