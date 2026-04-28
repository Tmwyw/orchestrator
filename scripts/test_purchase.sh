#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/test_purchase.sh <sku_id> <quantity> [user_id] [format]

Defaults: user_id=1, format=socks5_uri

Example:
  bash scripts/test_purchase.sh 1 5
  bash scripts/test_purchase.sh 1 10 42 json
EOF
  exit 1
}

[ $# -ge 2 ] || usage
SKU_ID="$1"
QUANTITY="$2"
USER_ID="${3:-1}"
FORMAT="${4:-socks5_uri}"

APP_HOME="${APP_HOME:-/opt/netrun-orchestrator}"
if [ -f "$APP_HOME/.env" ] && [ -z "${ORCHESTRATOR_API_KEY:-}" ]; then
  set -a; . "$APP_HOME/.env"; set +a
fi
[ -n "${ORCHESTRATOR_API_KEY:-}" ] || { echo "ERROR: ORCHESTRATOR_API_KEY not set" >&2; exit 2; }

API="http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/v1"
HDR=(-H "X-NETRUN-API-KEY: ${ORCHESTRATOR_API_KEY}" -H "Content-Type: application/json")

echo "=== 1. Reserve ==="
RESERVE_RESP=$(curl -fsS -X POST "$API/orders/reserve" "${HDR[@]}" \
  -d "{\"user_id\":${USER_ID},\"sku_id\":${SKU_ID},\"quantity\":${QUANTITY},\"reservation_ttl_sec\":300}")
echo "$RESERVE_RESP" | jq .
ORDER_REF=$(echo "$RESERVE_RESP" | jq -r '.order_ref')
[ "$ORDER_REF" != "null" ] && [ -n "$ORDER_REF" ] || { echo "Reserve failed"; exit 3; }

echo ""
echo "=== 2. Commit ==="
curl -fsS -X POST "$API/orders/${ORDER_REF}/commit" "${HDR[@]}" -d '{}' | jq .

echo ""
echo "=== 3. GET proxies (format=${FORMAT}) ==="
curl -fsS "$API/orders/${ORDER_REF}/proxies?format=${FORMAT}" \
  -H "X-NETRUN-API-KEY: ${ORCHESTRATOR_API_KEY}"

echo ""
echo "=== Done. order_ref=${ORDER_REF} ==="
