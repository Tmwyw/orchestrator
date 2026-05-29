#!/usr/bin/env bash
# scripts/vultr_node_watchdog.sh — DB-driven node watchdog. Wave PROVISION-1 ②.
#
# Replaces the old hardcoded `declare -A NODES` watchdog. Each tick it pulls the
# node list FROM THE DATABASE (id + url), pings each node's :8085/health, and —
# after N consecutive failures — reboots the node THROUGH THE ORCHESTRATOR
# (POST /v1/admin/nodes/{id}/reboot). The orchestrator resolves the node's Vultr
# account and decrypts that account's key; bash never touches a Vultr key
# (it only holds ORCHESTRATOR_API_KEY, which it already needs).
#
# This is the critical zero-terminal auto-recovery path: if it breaks, nodes
# stay down. See docs/wave_provision_2.md for the prod verification checklist.
#
# Config (env or sourced from $ENV_FILE):
#   DATABASE_URL          psql connection string (required)
#   ORCHESTRATOR_URL      base URL, default http://127.0.0.1:8090
#   ORCHESTRATOR_API_KEY  X-NETRUN-API-KEY for the reboot call (required)
#   WATCHDOG_FAIL_THRESHOLD   consecutive fails before reboot (default 5)
#   WATCHDOG_REBOOT_COOLDOWN  seconds between reboots of one node (default 3600)
#   WATCHDOG_PROBE_TIMEOUT    per-node /health curl timeout (default 5)

set -uo pipefail

ENV_FILE="${ENV_FILE:-/opt/netrun-orchestrator/.env}"
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://127.0.0.1:8090}"
FAIL_THRESHOLD="${WATCHDOG_FAIL_THRESHOLD:-5}"
REBOOT_COOLDOWN="${WATCHDOG_REBOOT_COOLDOWN:-3600}"
PROBE_TIMEOUT="${WATCHDOG_PROBE_TIMEOUT:-5}"
STATE_DIR="${WATCHDOG_STATE_DIR:-/var/lib/netrun-orchestrator/vultr-watchdog}"
LOG_TAG="vultr-node-watchdog"

log() { logger -t "$LOG_TAG" -- "$*"; printf '[%s] %s\n' "$LOG_TAG" "$*"; }
die() { log "FATAL: $*"; exit 1; }

command -v psql >/dev/null 2>&1 || die "psql_not_found"
command -v curl >/dev/null 2>&1 || die "curl_not_found"
[ -n "${DATABASE_URL:-}" ] || die "DATABASE_URL_not_set"
[ -n "${ORCHESTRATOR_API_KEY:-}" ] || die "ORCHESTRATOR_API_KEY_not_set"
mkdir -p "$STATE_DIR"

# Live node list: id<TAB>url. Skip 'disabled' nodes (admin took them out of
# rotation on purpose — rebooting them would fight that decision).
rows_raw="$(
  psql "$DATABASE_URL" -tAc \
    "select id || E'\t' || url from nodes
      where coalesce(runtime_status,'') <> 'disabled' and url is not null" \
    2>/dev/null
)" || die "db_query_failed"

if [ -z "$rows_raw" ]; then
  log "no active nodes in DB — nothing to probe"
  exit 0
fi

reboot_node() {
  local node_id="$1" code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 \
      -X POST "${ORCHESTRATOR_URL%/}/v1/admin/nodes/${node_id}/reboot" \
      -H "X-NETRUN-API-KEY: ${ORCHESTRATOR_API_KEY}" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then
    log "REBOOT ok node=$node_id (orchestrator resolved per-account key)"
    return 0
  fi
  log "REBOOT failed node=$node_id http=$code"
  return 1
}

now="$(date +%s)"

while IFS=$'\t' read -r node_id url; do
  if [ -z "$node_id" ] || [ -z "$url" ]; then
    continue
  fi
  fail_file="$STATE_DIR/${node_id}.fails"
  reboot_file="$STATE_DIR/${node_id}.last_reboot"
  fails="$(cat "$fail_file" 2>/dev/null || echo 0)"
  fails="${fails//[^0-9]/}"; : "${fails:=0}"

  if curl -s -f --max-time "$PROBE_TIMEOUT" -o /dev/null "${url%/}/health" 2>/dev/null; then
    if [ "$fails" -gt 0 ]; then
      log "recovery node=$node_id (was $fails fails)"
      echo 0 > "$fail_file"
    fi
    continue
  fi

  fails=$((fails + 1))
  echo "$fails" > "$fail_file"
  log "probe failed node=$node_id url=$url fails=$fails/$FAIL_THRESHOLD"

  if [ "$fails" -lt "$FAIL_THRESHOLD" ]; then
    continue
  fi

  last_reboot="$(cat "$reboot_file" 2>/dev/null || echo 0)"
  last_reboot="${last_reboot//[^0-9]/}"; : "${last_reboot:=0}"
  if [ $((now - last_reboot)) -lt "$REBOOT_COOLDOWN" ]; then
    log "node=$node_id at threshold but in reboot cooldown ($((now - last_reboot))/${REBOOT_COOLDOWN}s)"
    continue
  fi

  if reboot_node "$node_id"; then
    echo "$now" > "$reboot_file"
    echo 0 > "$fail_file"
  fi
done <<< "$rows_raw"

log "tick complete"
