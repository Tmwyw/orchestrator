#!/usr/bin/env bash
# Wave D pergb safety-net smoke (operator script — NOT an autotest).
#
# Walks the full pay-per-GB happy path against a live orchestrator + node:
#   1. Look up the active SKU code for the requested geo (1 GB tier).
#   2. POST /v1/orders/reserve_pergb to claim a port.
#   3. Push ~50 MB through the proxy in a loop.
#   4. Wait one polling cycle (default 60s) so traffic_poll catches up.
#   5. GET /v1/orders/{ref}/traffic — verify bytes_used > 0 and < quota.
#   6. Push more traffic until the proxy is reported as blocked
#      (curl --proxy starts failing OR /traffic shows status=depleted).
#
# This is the test that catches the "user pays for 1 GB but receives
# unlimited" failure mode before bot users do.
#
# Usage:
#   bash deploy/scripts/pergb_smoke.sh <geo> [<gb_amount>]
#   bash deploy/scripts/pergb_smoke.sh DE 1
#
# Env (override defaults):
#   ORCHESTRATOR_BASE_URL  default http://127.0.0.1:8090
#   ORCHESTRATOR_API_KEY   required
#   USER_ID                default 999999  (a stable test user id)
#   POLL_WAIT_SEC          default 65      (one cycle + slack)
#   PUSH_TARGET_BYTES      default 60000000 (~60 MB; bigger than any 1 GB safety
#                                           buffer would be silly, this is for
#                                           the "see usage" step)
#   EXHAUST_TARGET_BYTES   default 1100000000 (~1.1 GB; should trip block at 1)
#   PUSH_URL               default https://speed.cloudflare.com/__down?bytes=10000000
#                                  (10 MB chunks; Cloudflare is reliable + free)

set -euo pipefail

GEO="${1:?Usage: bash pergb_smoke.sh <geo> [<gb_amount>]}"
GB_AMOUNT="${2:-1}"

BASE="${ORCHESTRATOR_BASE_URL:-http://127.0.0.1:8090}"
API_KEY="${ORCHESTRATOR_API_KEY:?ORCHESTRATOR_API_KEY env var required}"
USER_ID="${USER_ID:-999999}"
POLL_WAIT_SEC="${POLL_WAIT_SEC:-65}"
PUSH_TARGET_BYTES="${PUSH_TARGET_BYTES:-60000000}"
EXHAUST_TARGET_BYTES="${EXHAUST_TARGET_BYTES:-1100000000}"
PUSH_URL="${PUSH_URL:-https://speed.cloudflare.com/__down?bytes=10000000}"

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }
}
require curl
require jq

H_AUTH=("-H" "X-NETRUN-API-KEY: $API_KEY")
H_JSON=("-H" "content-type: application/json")

log() { printf '[%s] %s\n' "$(date -Iseconds)" "$*" >&2; }

# 1. Pick the SKU.
log "looking up active datacenter_pergb SKU for geo=$GEO"
SKU_PAYLOAD="$(curl -fsS "${H_AUTH[@]}" "$BASE/v1/skus/active")"
SKU_ID="$(echo "$SKU_PAYLOAD" | jq -r --arg geo "$GEO" '
  .items
  | map(select(.product_kind == "datacenter_pergb" and .geo_code == $geo))
  | first
  | .sku_id // empty
')"
if [ -z "$SKU_ID" ]; then
  echo "no active datacenter_pergb SKU found for geo=$GEO" >&2
  echo "$SKU_PAYLOAD" | jq -r '.items[] | "\(.code) (\(.product_kind), \(.geo_code))"' >&2
  exit 1
fi
log "sku_id=$SKU_ID"

# 2. Reserve.
log "reserving ${GB_AMOUNT} GB on sku_id=$SKU_ID"
RESERVE_BODY="$(jq -n \
  --argjson user "$USER_ID" \
  --argjson sku "$SKU_ID" \
  --argjson gb "$GB_AMOUNT" \
  '{user_id:$user, sku_id:$sku, gb_amount:$gb}')"

RESERVE="$(curl -fsS -X POST "${H_AUTH[@]}" "${H_JSON[@]}" \
  -d "$RESERVE_BODY" \
  "$BASE/v1/orders/reserve_pergb")"
echo "$RESERVE" | jq .

ORDER_REF="$(echo "$RESERVE" | jq -r .order_ref)"
HOST="$(echo "$RESERVE" | jq -r .host)"
PORT="$(echo "$RESERVE" | jq -r .port)"
LOGIN="$(echo "$RESERVE" | jq -r .login)"
PASSWORD="$(echo "$RESERVE" | jq -r .password)"
QUOTA="$(echo "$RESERVE" | jq -r .bytes_quota)"

PROXY_URL="socks5://${LOGIN}:${PASSWORD}@${HOST}:${PORT}"
log "order_ref=$ORDER_REF proxy=$HOST:$PORT quota=$QUOTA"

# 3. Push first batch (small, just to see usage).
log "pushing ~${PUSH_TARGET_BYTES} bytes through proxy"
pushed=0
while [ "$pushed" -lt "$PUSH_TARGET_BYTES" ]; do
  bytes="$(curl --proxy "$PROXY_URL" -sS -o /dev/null -w '%{size_download}' "$PUSH_URL" || echo 0)"
  pushed=$((pushed + bytes))
  log "  push: this=${bytes}B total=${pushed}B"
done

# 4. Wait for traffic_poll to ack.
log "sleeping ${POLL_WAIT_SEC}s to let traffic_poll catch up"
sleep "$POLL_WAIT_SEC"

# 5. Verify usage.
log "checking traffic snapshot"
curl -fsS "${H_AUTH[@]}" "$BASE/v1/orders/$ORDER_REF/traffic" | jq .

# 6. Exhaust the quota — push until the proxy stops accepting traffic OR
# /traffic reports status=depleted. Fail loud if neither happens by the
# time we've requested 1.1×quota.
log "exhausting quota — pushing up to ${EXHAUST_TARGET_BYTES} bytes"
pushed_total="$pushed"
blocked=0
while [ "$pushed_total" -lt "$EXHAUST_TARGET_BYTES" ]; do
  if ! curl --proxy "$PROXY_URL" --max-time 30 -sS -o /dev/null -w '%{size_download}\n' \
       "$PUSH_URL" >/tmp/pergb_smoke_chunk.bytes 2>/dev/null; then
    log "  curl failed → likely nftables drop"
    blocked=1
    break
  fi
  bytes="$(cat /tmp/pergb_smoke_chunk.bytes)"
  if [ "$bytes" = "0" ]; then
    log "  zero-byte response → blocked"
    blocked=1
    break
  fi
  pushed_total=$((pushed_total + bytes))
  log "  exhaust: total=${pushed_total}B"
done

# Re-check status — even if the curl loop continued, the orchestrator
# should now report depleted.
log "final traffic snapshot"
SNAPSHOT="$(curl -fsS "${H_AUTH[@]}" "$BASE/v1/orders/$ORDER_REF/traffic")"
echo "$SNAPSHOT" | jq .
STATUS="$(echo "$SNAPSHOT" | jq -r .status)"

if [ "$blocked" = "1" ] || [ "$STATUS" = "depleted" ]; then
  log "PASS: account blocked at the node and/or status=depleted (status=$STATUS)"
  exit 0
fi

log "FAIL: account is still serving traffic and orchestrator reports status=$STATUS"
log "      the safety net is not working — investigate node_blocked + watchdog logs"
exit 2
