#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/enroll-node.sh <agent_url> [options]

Options:
  --api-key VALUE   Pass node-agent API key (if node requires it)
  --name VALUE      Override auto-generated node name
  --geo VALUE       Override geo_code (use when /describe returns null geo)
  --force           Save node even if health is not ready
  --auto-bind       Bind to all active SKUs with matching geo

Example:
  bash scripts/enroll-node.sh http://10.0.0.5:8085
  bash scripts/enroll-node.sh http://10.0.0.5:8085 --geo US --auto-bind
  bash scripts/enroll-node.sh http://10.0.0.5:8085 --api-key SECRET --name node-de-1
EOF
  exit 1
}

[ $# -ge 1 ] || usage
URL="$1"; shift

API_KEY=""
NAME=""
GEO=""
FORCE="false"
AUTO_BIND="false"

while [ $# -gt 0 ]; do
  case "$1" in
    --api-key)   API_KEY="${2:?--api-key requires value}";  shift 2 ;;
    --name)      NAME="${2:?--name requires value}";        shift 2 ;;
    --geo)       GEO="${2:?--geo requires value}";          shift 2 ;;
    --force)     FORCE="true";     shift ;;
    --auto-bind) AUTO_BIND="true"; shift ;;
    -h|--help)   usage ;;
    *) echo "ERROR: unknown option: $1" >&2; usage ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
ENV_FILE="${ORCHESTRATOR_ENV_FILE:-$REPO_ROOT/.env}"
if [ ! -f "$ENV_FILE" ] && [ -f /opt/netrun-orchestrator/.env ]; then
  ENV_FILE="/opt/netrun-orchestrator/.env"
fi
if [ -f "$ENV_FILE" ] && [ -z "${ORCHESTRATOR_API_KEY:-}" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
[ -n "${ORCHESTRATOR_API_KEY:-}" ] || { echo "ERROR: ORCHESTRATOR_API_KEY not set" >&2; exit 2; }

ORCH_PORT="${ORCHESTRATOR_PORT:-8090}"

payload="$(jq -n \
  --arg url "$URL" \
  --arg api_key "$API_KEY" \
  --arg name "$NAME" \
  --arg geo "$GEO" \
  --argjson force "$FORCE" \
  --argjson auto_bind "$AUTO_BIND" \
  '{agent_url:$url, force:$force, auto_bind_active_skus:$auto_bind}
   + (if $api_key == "" then {} else {api_key:$api_key} end)
   + (if $name    == "" then {} else {name:$name}      end)
   + (if $geo     == "" then {} else {geo_code:$geo}   end)')"

curl -fsS -X POST "http://127.0.0.1:${ORCH_PORT}/v1/nodes/enroll" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary "$payload" | jq .
