#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/add_sku.sh <code> <product_kind> <geo_code> <protocol> <duration_days> <price_per_piece> <target_stock> <refill_batch_size>

Example:
  bash scripts/add_sku.sh ipv6_us_socks5 ipv6 US socks5 30 0.14 100 50
  bash scripts/add_sku.sh ipv6_de_socks5 ipv6 DE socks5 30 0.14 100 50

Requires:
  - DATABASE_URL env (or .env in /opt/netrun-orchestrator/)
  - jq, psql installed
EOF
  exit 1
}

if [ $# -ne 8 ]; then usage; fi

CODE="$1"
PRODUCT_KIND="$2"
GEO_CODE="$3"
PROTOCOL="$4"
DURATION_DAYS="$5"
PRICE_PER_PIECE="$6"
TARGET_STOCK="$7"
REFILL_BATCH_SIZE="$8"

APP_HOME="${APP_HOME:-/opt/netrun-orchestrator}"
if [ -f "$APP_HOME/.env" ] && [ -z "${DATABASE_URL:-}" ]; then
  set -a; . "$APP_HOME/.env"; set +a
fi
[ -n "${DATABASE_URL:-}" ] || { echo "ERROR: DATABASE_URL not set" >&2; exit 2; }

case "$PRODUCT_KIND" in ipv6|datacenter_pergb) ;; *) echo "ERROR: product_kind must be ipv6 or datacenter_pergb" >&2; exit 2 ;; esac
case "$PROTOCOL" in socks5|http) ;; *) echo "ERROR: protocol must be socks5 or http" >&2; exit 2 ;; esac

psql "$DATABASE_URL" <<SQL
INSERT INTO skus (code, product_kind, geo_code, protocol, duration_days, price_per_piece, target_stock, refill_batch_size)
VALUES ('${CODE}', '${PRODUCT_KIND}', '${GEO_CODE}', '${PROTOCOL}', ${DURATION_DAYS}, ${PRICE_PER_PIECE}, ${TARGET_STOCK}, ${REFILL_BATCH_SIZE})
ON CONFLICT (code) DO UPDATE SET
  target_stock = EXCLUDED.target_stock,
  refill_batch_size = EXCLUDED.refill_batch_size,
  price_per_piece = EXCLUDED.price_per_piece,
  updated_at = NOW()
RETURNING id, code, target_stock;
SQL
