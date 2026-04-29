# NETRUN Orchestrator — Operations

This guide covers a fresh install, day-to-day operations, and basic
troubleshooting for the orchestrator service.

---

## 1. Fresh install on Ubuntu 22.04 / 24.04

One-liner (run as root):

```bash
git clone https://github.com/Tmwyw/orchestrator.git /opt/netrun-orchestrator
cd /opt/netrun-orchestrator
bash install_orchestrator.sh
```

What the installer does:

1. `apt-get install` → `python3`, `python3-venv`, `python3-pip`, `curl`, `jq`,
   `ca-certificates`, `redis-server`, and (unless `--external-db`) `postgresql`.
2. Generates `/opt/netrun-orchestrator/.env` with random `ORCHESTRATOR_API_KEY`
   and `DB_PASSWORD`. Existing `.env` is preserved on re-runs.
3. Creates a local PostgreSQL role and database
   (`netrun_orchestrator` / `netrun_orchestrator`) — skipped with `--external-db`.
4. Enables and starts `redis-server`; verifies via `redis-cli ping`.
5. Builds `.venv`, installs `requirements.txt`.
6. Runs `python -m orchestrator.migrate` against `DATABASE_URL`.
7. Installs and starts two systemd units:
   - `netrun-orchestrator.service` — FastAPI on `${ORCHESTRATOR_PORT}` (default 8090)
   - `netrun-orchestrator-worker.service` — generation worker
8. Polls `GET /health` until ready.

**External Postgres:**

```bash
DATABASE_URL=postgresql://user:pass@db.example.com:5432/netrun \
  bash install_orchestrator.sh --external-db
```

---

## 2. Configuration (`.env`)

The installer creates `/opt/netrun-orchestrator/.env`. See `.env.example` for a
template. Key variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `ORCHESTRATOR_API_KEY` | random 48 chars | Required for every API call (`X-NETRUN-API-KEY` header) |
| `ORCHESTRATOR_HOST` | `0.0.0.0` | Bind address |
| `ORCHESTRATOR_PORT` | `8090` | HTTP port |
| `DATABASE_URL` | (generated) | `postgresql://user:pass@host:5432/db` |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Used for reservation TTL and idempotency cache |
| `JOBS_ROOT` | `/opt/netrun-orchestrator/jobs` | Where simple-job `proxies.list` files are written |
| `NODE_REQUEST_TIMEOUT_SEC` | `1200` | Timeout for node `/generate` calls |
| `WORKER_POLL_INTERVAL_SEC` | `2` | Generation worker poll interval |
| `PROXY_REFILL_INTERVAL_SEC` | `30` | Refill scheduler interval |
| `REFILL_DEFAULT_PRIORITY` | `10` | Priority of refill jobs |
| `REFILL_MAX_SKUS_PER_CYCLE` | `100` | Soft limit per refill pass |
| `PROXY_ALLOW_DEGRADED_NODES` | `false` | Include `degraded` nodes in refill / allocator |
| `VALIDATION_BATCH_SIZE` | `50` | Validation worker claim batch size |
| `VALIDATION_CONCURRENCY` | `20` | Concurrent probes per cycle |
| `VALIDATION_POLL_INTERVAL_SEC` | `5` | Validation worker poll interval |
| `VALIDATION_STRICT_SSL` | `true` | When `false`, SOCKS5/HTTP probe skips SSL cert verification. Set to `false` only in test environments with self-signed certs. |
| `RESERVATION_DEFAULT_TTL_SEC` | `300` | Default TTL when client omits `reservation_ttl_sec` |
| `RESERVATION_MIN_TTL_SEC` | `30` | Lower clamp |
| `RESERVATION_MAX_TTL_SEC` | `3600` | Upper clamp |
| `ORCHESTRATOR_START_PORT_MIN` | `32000` | Lower bound for per-node port allocation |
| `ORCHESTRATOR_START_PORT_MAX` | `65000` | Upper bound |

After editing `.env`, restart services:

```bash
systemctl restart netrun-orchestrator netrun-orchestrator-worker
```

---

## 3. Adding a node

### Recommended: auto-enroll (Wave B-6)

```bash
cd /opt/netrun-orchestrator
bash scripts/enroll-node.sh http://NODE_IP:8085
```

The orchestrator calls the node's `GET /describe` (Wave B-6.1) and
`GET /health`, then deterministically derives `node_id` from the URL,
auto-fills `geo_code`, `capacity`, `generator_script`,
`max_parallel_jobs`, and `max_batch_size`, and UPSERTs the row. Re-running
on the same URL is idempotent.

Common options:

```bash
# Auto-bind to every active SKU with the same geo:
bash scripts/enroll-node.sh http://10.0.0.5:8085 --auto-bind

# Force-save even if /health is not ready (status=unavailable):
bash scripts/enroll-node.sh http://10.0.0.5:8085 --force

# Override geo when /describe returns null:
bash scripts/enroll-node.sh http://10.0.0.5:8085 --geo DE

# Pass node-agent X-API-KEY (only if NODE_AGENT_API_KEY was set on the node):
bash scripts/enroll-node.sh http://10.0.0.5:8085 --api-key SECRET --name node-de-1
```

Error responses:

| HTTP | error                       | Meaning |
|------|-----------------------------|---------|
| 502  | `describe_unreachable`      | `GET /describe` failed (network, agent down, wrong URL) |
| 400  | `api_key_required_by_node`  | Node requires `X-API-KEY` but `--api-key` was not passed |
| 409  | `health_unreachable`        | `/health` did not respond — pass `--force` to save anyway |
| 409  | `node_health_not_ready`     | Health responded but `success/status/ipv6` failed; `extra.diagnostics` shows what |

### Manual fallback (legacy)

`scripts/add_node.sh` is still available for environments where the node
runtime predates `/describe` (no Wave B-6.1):

```bash
bash scripts/add_node.sh <url> [name] [geo] [capacity] [node_api_key] [force]
```

It POSTs to `/nodes` (no `/v1` prefix — see §9 footnote) and only checks
`/health`. Pass `true` as the 6th argument to bypass the health check.

Verify:

```bash
. /opt/netrun-orchestrator/.env
curl -fsS -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  "http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/nodes" | jq .
```

---

## 4. Adding a SKU and binding it to nodes

Two scripts wrap the SQL upserts (`scripts/add_sku.sh` and
`scripts/bind_node.sh`). Both read `DATABASE_URL` from
`/opt/netrun-orchestrator/.env` if not already in the environment.

Full workflow for one geo:

```bash
# 1. Define the SKU
bash scripts/add_sku.sh ipv6_us_socks5 ipv6 US socks5 30 0.14 100 50
#                        code           kind  geo proto days price stock batch

# 2. Bind it to a node
bash scripts/bind_node.sh ipv6_us_socks5 node-de-1
# Defaults: weight=100, max_batch_size=1500. Override:
# bash scripts/bind_node.sh ipv6_us_socks5 node-de-1 100 50

# 3. Bind to additional nodes (equal-share allocator will spread orders)
bash scripts/bind_node.sh ipv6_us_socks5 node-de-2
bash scripts/bind_node.sh ipv6_us_socks5 node-fr-1
```

`product_kind` must be `ipv6` or `datacenter_pergb`; `protocol` must be
`socks5` or `http` (DB CHECK constraints enforce this).

---

## 5. Schedulers + watchdog (Wave B-7a)

`install_orchestrator.sh` installs and starts five systemd units in total:

| Unit | Purpose |
|------|---------|
| `netrun-orchestrator.service` | FastAPI process |
| `netrun-orchestrator-worker.service` | generation worker |
| `netrun-orchestrator-refill.service` | refill scheduler (one-shot per `PROXY_REFILL_INTERVAL_SEC`) |
| `netrun-orchestrator-validation.service` | proxy validation loop |
| `netrun-orchestrator-watchdog.service` | recovers stuck `running` jobs, releases expired reservations, invalidates stale `pending_validation`, clears expired delivery content |

The wrappers `bash scripts/start_schedulers.sh` / `scripts/stop_schedulers.sh`
auto-detect systemd: if all three (`refill`, `validation`, `watchdog`) units
are installed, they call `systemctl restart`/`stop`; otherwise they fall back
to legacy `screen` sessions (development boxes only).

Inspect:

```bash
systemctl status netrun-orchestrator-refill netrun-orchestrator-validation \
                 netrun-orchestrator-watchdog --no-pager
journalctl -u netrun-orchestrator-watchdog -f
```

Watchdog tunables (in `.env`, all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `WATCHDOG_INTERVAL_SEC` | `60` | Loop interval (clamped to ≥10) |
| `WATCHDOG_RUNNING_TIMEOUT_SEC` | `1800` | Mark `jobs.status='running'` rows older than this as `failed` |
| `WATCHDOG_PENDING_VALIDATION_TIMEOUT_SEC` | `600` | Mark `proxy_inventory.status='pending_validation'` older than this as `invalid` |

---

## 6. End-to-end smoke test

```bash
bash scripts/test_purchase.sh <sku_id> <quantity> [user_id] [format]
```

Example:

```bash
bash scripts/test_purchase.sh 1 5
bash scripts/test_purchase.sh 1 10 42 json
```

The script runs three calls in sequence:

1. `POST /v1/orders/reserve` → returns `order_ref`.
2. `POST /v1/orders/{order_ref}/commit` → marks the order committed and
   inventory `sold`.
3. `GET /v1/orders/{order_ref}/proxies?format=...` → prints proxies in the
   requested format.

Common failure modes:

| Error | Meaning |
|-------|---------|
| `sku_not_active` | SKU does not exist or `is_active = false` |
| `no_active_bindings` | SKU has no `sku_node_bindings` rows with active node |
| `insufficient_stock` | Not enough `proxy_inventory.status='available'` rows; `available_now` reports current pool |
| `reservation_expired` | `commit` called after `expires_at` (TTL elapsed) |
| `format_locked` | A prior `proxies` request locked a different format for this order |

---

## 7. Inspection / monitoring

Useful SQL probes (run as `psql "$DATABASE_URL"`):

```sql
-- Pool size per SKU and status
SELECT s.code, i.status, COUNT(*) AS n
FROM proxy_inventory i JOIN skus s ON s.id = i.sku_id
GROUP BY s.code, i.status
ORDER BY s.code, i.status;

-- In-flight jobs by status
SELECT status, COUNT(*) FROM jobs
WHERE status IN ('queued','running')
GROUP BY status;

-- Recent jobs (last 20)
SELECT id, status, count, product, sku_id, reason, node_id, created_at
FROM jobs ORDER BY created_at DESC LIMIT 20;

-- Recent orders (last 20)
SELECT order_ref, status, sku_id, requested_count, allocated_count,
       reserved_at, expires_at, committed_at
FROM orders ORDER BY created_at DESC LIMIT 20;

-- Active reservations (still holding inventory)
SELECT order_ref, sku_id, allocated_count, expires_at
FROM orders WHERE status = 'reserved' AND expires_at > now()
ORDER BY expires_at ASC;

-- Refill backlog: pending validation per SKU
SELECT s.code, COUNT(*) FROM proxy_inventory i
JOIN skus s ON s.id = i.sku_id
WHERE i.status = 'pending_validation'
GROUP BY s.code;
```

Service logs:

```bash
journalctl -u netrun-orchestrator -f          # API process
journalctl -u netrun-orchestrator-worker -f   # generation worker
screen -r netrun-refill                       # refill loop output
screen -r netrun-validation                   # validation loop output
```

### Structured logs (Wave B-7b.1)

Since Wave B-7b.1 the API + 4 schedulers emit JSON to stderr (one event
per line). Use `jq` to filter:

```bash
journalctl -u netrun-orchestrator -o cat | jq 'select(.level == "error")'
journalctl -u netrun-orchestrator-watchdog -o cat | jq 'select(.event | startswith("watchdog_"))'
```

Common fields: `event`, `level`, `logger`, `timestamp`. Service-specific
context fields: `order_ref`, `sku_id`, `node_id`, `job_id`.

### Prometheus metrics (Wave B-7b.2)

Orchestrator exposes Prometheus exposition format on `/metrics` (no
auth — protected by network boundary, see B-7b.5 nginx ACL). Key
metrics:

| Metric | Type | Labels |
|---|---|---|
| `netrun_reserve_total` | counter | status, error |
| `netrun_reserve_duration_sec` | histogram | — |
| `netrun_commit_total` | counter | status |
| `netrun_release_total` | counter | status |
| `netrun_scheduler_run_total` | counter | scheduler, status |
| `netrun_scheduler_run_duration_sec` | histogram | scheduler |
| `netrun_watchdog_actions_total` | counter | action |
| `netrun_http_requests_total` | counter | method, path, status |
| `netrun_http_duration_sec` | histogram | method, path |

Example scrape config (Prometheus side):

```yaml
scrape_configs:
  - job_name: netrun-orchestrator
    static_configs:
      - targets: ['127.0.0.1:8090']
    metrics_path: /metrics
    scrape_interval: 15s
```

Verify locally:

```bash
curl -s http://127.0.0.1:8090/metrics | grep -E '^netrun_' | head -20
```

Redis introspection:

```bash
redis-cli KEYS 'reservation:*'                # active reservations
redis-cli GET reservation:ord_abc123          # reservation payload
redis-cli KEYS 'idem:reserve:*'               # cached idempotent responses
redis-cli MONITOR                             # live command stream (debug only)
```

---

## 8. Common operations

Extend an order's lease (full inventory or a subset):

```bash
. /opt/netrun-orchestrator/.env
ORD=ord_abcd12345678   # from previous reserve/commit

# Whole order, +30 days
curl -fsS -X POST "http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/v1/orders/$ORD/extend" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"duration_days":30}' | jq .

# Only US proxies in this order
curl -fsS -X POST "http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/v1/orders/$ORD/extend" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"duration_days":15,"geo_code":"US"}' | jq .
```

Release a reservation (returns inventory to the pool):

```bash
curl -fsS -X POST "http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/v1/orders/$ORD/release" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" | jq .
```

Inspect an order:

```bash
curl -fsS "http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/v1/orders/$ORD" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" | jq .
```

### Admin endpoints (Wave B-7b.3)

Three read-only admin endpoints are available under `/v1/admin/*` with the
standard `X-NETRUN-API-KEY` header. All three serialize Decimal fields as
strings (see `wave_b_design.md` § 6.10 — Decimal serialization convention).

Daily sales summary:

```bash
. /opt/netrun-orchestrator/.env
curl -fsS "http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/v1/admin/stats?range_days=7" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" | jq .
```

Search a user's orders:

```bash
curl -fsS "http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/v1/admin/orders?user_id=42" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" | jq '.items[] | {order_ref, status, allocated_count}'
```

Export archived proxies for accounting (date range required):

```bash
curl -fsS "http://127.0.0.1:${ORCHESTRATOR_PORT:-8090}/v1/admin/archive?from_date=2026-01-01&to_date=2026-04-30&geo=US" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" | jq '.count'
```

Archive output is capped at 10000 rows; orders search at 1000. Use date
ranges or status filter to paginate.

---

## 9. Endpoint paths

Since Wave B-7a all endpoints are available under the unified `/v1/` prefix.
The legacy unprefixed paths (`/health`, `/nodes`, `/jobs/...`) still work for
backward compatibility and will be removed in the next major version — new
clients and scripts should use `/v1/*`.

**`/v1/` (canonical):**

- `GET    /v1/health`
- `GET    /v1/nodes`
- `POST   /v1/nodes`
- `DELETE /v1/nodes/{node_id}`
- `POST   /v1/nodes/enroll`
- `POST   /v1/jobs`
- `GET    /v1/jobs/{job_id}`
- `GET    /v1/jobs/{job_id}/proxies.list`
- `POST   /v1/orders/reserve`
- `POST   /v1/orders/{order_ref}/commit`
- `POST   /v1/orders/{order_ref}/release`
- `POST   /v1/orders/{order_ref}/extend`
- `GET    /v1/orders/{order_ref}`
- `GET    /v1/orders/{order_ref}/proxies?format=socks5_uri|host_port_user_pass|user_pass_at_host_port|json`

**Legacy aliases (deprecated):** `/health`, `/nodes`, `/nodes/{id}`,
`/jobs`, `/jobs/{id}`, `/jobs/{id}/proxies.list` — same handlers, scheduled
for removal.

All endpoints require `X-NETRUN-API-KEY` matching `ORCHESTRATOR_API_KEY`.
