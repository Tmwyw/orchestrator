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
NODE_URL="${NODE_URL:-http://127.0.0.1:8085}"
NODE_NAME="${NODE_NAME:-smoke-node}"
NODE_GEO="${NODE_GEO:-smoke}"
NODE_CAPACITY="${NODE_CAPACITY:-1000}"
IDEMPOTENCY_KEY="${IDEMPOTENCY_KEY:-smoke-$(date +%s)}"
OUT_FILE="${OUT_FILE:-/tmp/netrun-smoke-proxies.list}"

node_payload="$(jq -n \
  --arg name "$NODE_NAME" \
  --arg url "$NODE_URL" \
  --arg geo "$NODE_GEO" \
  --argjson capacity "$NODE_CAPACITY" \
  '{name:$name,url:$url,geo:$geo,capacity:$capacity}')"

curl -fsS -X POST "$ORCH_URL/nodes" \
  -H "X-NETRUN-API-KEY: $API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary "$node_payload" >/dev/null

job_payload="$(jq -n --arg idempotency_key "$IDEMPOTENCY_KEY" '{count:10,product:"smoke",idempotency_key:$idempotency_key}')"
job_response="$(curl -fsS -X POST "$ORCH_URL/jobs" \
  -H "X-NETRUN-API-KEY: $API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary "$job_payload")"

printf '%s\n' "$job_response" | jq -e '.success == true and (.status == "queued" or .status == "running" or .status == "success")' >/dev/null
job_id="$(printf '%s\n' "$job_response" | jq -r '.job.id')"

status=""
for _ in $(seq 1 240); do
  job_status="$(curl -fsS "$ORCH_URL/jobs/$job_id" -H "X-NETRUN-API-KEY: $API_KEY")"
  status="$(printf '%s\n' "$job_status" | jq -r '.job.status')"
  if [ "$status" = "success" ] || [ "$status" = "failed" ]; then
    break
  fi
  sleep 2
done

if [ "$status" != "success" ]; then
  printf '%s\n' "$job_status" | jq .
  if [ "$(printf '%s\n' "$job_status" | jq -r '.job.error // empty')" = "node_response_missing_items" ]; then
    echo "node-agent /generate must return compatible items[]; node-local output.proxiesListPath is not readable by orchestrator" >&2
  fi
  echo "job did not succeed: $status" >&2
  exit 1
fi

printf '%s\n' "$job_status" | jq -e '
  .job.profile.ipv6_policy == "ipv6_only"
  and .job.profile.network_profile == "high_compatibility"
  and .job.profile.fingerprint_profile_version == "v2_android_ipv6_only_dns_custom"
  and .job.profile.intended_client_os_profile == "android_mobile"
  and .job.profile.actual_client_profile == "not_controlled_by_proxy"
  and .job.profile.effective_client_os_profile == "not_controlled_by_proxy"
' >/dev/null

curl -fsS "$ORCH_URL/jobs/$job_id/proxies.list" \
  -H "X-NETRUN-API-KEY: $API_KEY" \
  -o "$OUT_FILE"

[ -s "$OUT_FILE" ] || { echo "empty proxies.list" >&2; exit 1; }
first_line="$(awk 'NF { print; exit }' "$OUT_FILE")"
if ! [[ "$first_line" =~ ^[^:]+:[0-9]{1,5}:[^:]+:[^:]+$ ]]; then
  echo "invalid first proxy line: $first_line" >&2
  exit 1
fi

printf 'job_id=%s\n' "$job_id"
printf 'proxies_list=%s\n' "$OUT_FILE"
head -n 3 "$OUT_FILE"
