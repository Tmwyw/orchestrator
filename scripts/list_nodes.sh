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

curl -fsS "$ORCH_URL/v1/nodes" -H "X-NETRUN-API-KEY: $API_KEY" | jq .
