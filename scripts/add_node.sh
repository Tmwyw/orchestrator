#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
ENV_FILE="${ORCHESTRATOR_ENV_FILE:-$REPO_ROOT/.env}"
if [ ! -f "$ENV_FILE" ] && [ -f /opt/netrun-orchestrator/.env ]; then
  ENV_FILE="/opt/netrun-orchestrator/.env"
fi

[ -f "$ENV_FILE" ] || { echo "missing env file" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

ORCH_URL="${ORCH_URL:-http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}}"
API_KEY="${ORCHESTRATOR_API_KEY:?ORCHESTRATOR_API_KEY is required}"

NODE_URL="${1:?usage: add_node.sh <url> [name] [geo] [capacity] [node_api_key] [force]}"
NODE_NAME="${2:-node-$(date +%s)}"
NODE_GEO="${3:-}"
NODE_CAPACITY="${4:-1000}"
NODE_API_KEY="${5:-}"
FORCE="${6:-false}"

payload="$(jq -n \
  --arg name "$NODE_NAME" \
  --arg url "$NODE_URL" \
  --arg geo "$NODE_GEO" \
  --argjson capacity "$NODE_CAPACITY" \
  --arg api_key "$NODE_API_KEY" \
  --argjson force "$FORCE" \
  '{name:$name,url:$url,geo:$geo,capacity:$capacity,force:$force} + (if $api_key == "" then {} else {api_key:$api_key} end)')"

curl -fsS -X POST "$ORCH_URL/v1/nodes" \
  -H "X-NETRUN-API-KEY: $API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary "$payload" | jq .
