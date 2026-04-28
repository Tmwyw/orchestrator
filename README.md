# NETRUN Orchestrator

Production control-plane for NETRUN proxy nodes.

This repo is intentionally separate from node runtime and Telegram bot code. It manages nodes, accepts jobs, stores job state, calls node-agent over HTTP, and saves generated `proxies.list` files as the source of truth.

## Install

Fresh Ubuntu 22.04/24.04:

```bash
git clone <orchestrator-repo-url> /opt/netrun-orchestrator
cd /opt/netrun-orchestrator
bash install_orchestrator.sh
```

External PostgreSQL:

```bash
cp .env.example .env
editor .env
bash install_orchestrator.sh --external-db
```

The installer creates `/opt/netrun-orchestrator`, installs Python dependencies in `.venv`, runs migrations, installs `netrun-orchestrator.service` and `netrun-orchestrator-worker.service`, and waits for authenticated `/health`.

## API Auth

All endpoints require:

```text
X-NETRUN-API-KEY: <key>
```

The generated key is stored in:

```text
/opt/netrun-orchestrator/.env
```

## API

```text
GET    /health
GET    /nodes
POST   /nodes
DELETE /nodes/{id}
POST   /jobs
GET    /jobs/{id}
GET    /jobs/{id}/proxies.list
```

Add a node:

```bash
bash scripts/add_node.sh http://NODE_IP:8085 node-1 US 1000
```

`POST /nodes` checks node-agent `/health` before saving. The health response must have `success=true`, `status=ready`, and either `ipv6.ok=true` or `ipv6Egress.ok=true`. To save an unavailable node for later repair, pass `force=true`.

Create a job:

```bash
curl -fsS -X POST http://127.0.0.1:8090/jobs \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  -H "Content-Type: application/json" \
  --data-binary '{"count":10,"product":"android_ipv6_only"}' | jq .
```

Download result:

```bash
curl -fsS http://127.0.0.1:8090/jobs/<job_id>/proxies.list \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  -o proxies.list
```

## Product Contract

Clients cannot pass technical generation parameters. `POST /jobs` accepts only `android_ipv6_only` and `smoke` products:

```json
{
  "count": 10,
  "product": "android_ipv6_only",
  "idempotency_key": "optional-retry-key"
}
```

Any other product returns `invalid_product`.

`idempotency_key` is optional. If it already exists, the API returns the existing job instead of creating a duplicate.

The orchestrator always sends this production contract to nodes:

```text
ipv6_policy=ipv6_only
network_profile=high_compatibility
fingerprint_profile_version=v2_android_ipv6_only_dns_custom
intended_client_os_profile=android_mobile
client_os_profile_enforcement=not_controlled_by_proxy
actual_client_profile=not_controlled_by_proxy
effective_client_os_profile=not_controlled_by_proxy
```

Any attempt to pass fields such as `ipv6Policy`, `networkProfile`, `fingerprintProfileVersion`, `generatorScript`, or `startPort` to `POST /jobs` is rejected with `invalid_product_contract`.

`POST /jobs` returns immediately with a queued job. The worker service processes queued jobs asynchronously. Poll `GET /jobs/{id}` until `status` is `success` or `failed`, then download `/jobs/{id}/proxies.list`.

## Node Response Compatibility

The orchestrator requires node-agent `/generate` to return `items[]` with proxy objects:

```json
{
  "success": true,
  "status": "ready",
  "items": [
    {"host": "1.2.3.4", "port": 32000, "login": "user", "password": "pass"}
  ]
}
```

Node-agent responses that only include `generatedCount`, `output.proxiesListPath`, `jobDir`, or `logs` are not enough for remote orchestration because the orchestrator cannot read node-local files. If `items[]` is missing or too short, the worker marks the job failed with `node_response_missing_items` and logs response diagnostics.

## Smoke

Requires a running node-agent, default `http://127.0.0.1:8085`:

```bash
NODE_URL=http://NODE_IP:8085 bash scripts/smoke_refill.sh
```

The smoke script adds a test node, creates a 10-proxy async job, polls until completion, downloads `proxies.list`, validates `ip:port:login:pass` format, and therefore verifies that the node runtime returns compatible `items[]`. Default orchestrator allocation starts at port `32000` to avoid direct node runtime smoke jobs that use `30000`.

## Operations

```bash
bash scripts/list_nodes.sh
bash scripts/health_nodes.sh
bash scripts/run_worker_once.sh
systemctl status netrun-orchestrator --no-pager
systemctl status netrun-orchestrator-worker --no-pager
journalctl -u netrun-orchestrator -f
journalctl -u netrun-orchestrator-worker -f
```

## Scope

This repository does not contain node runtime, 3proxy build logic, Telegram bot code, database business logic from the bot, SKU/payment/inventory code, or legacy archives. The orchestrator talks to proxy nodes only through HTTP.
