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

## 5. Schedulers + watchdog (Wave B-7a, extended in B-8.2)

`install_orchestrator.sh` installs and starts six systemd units in total:

| Unit | Purpose |
|------|---------|
| `netrun-orchestrator.service` | FastAPI process |
| `netrun-orchestrator-worker.service` | generation worker |
| `netrun-orchestrator-refill.service` | refill scheduler (one-shot per `PROXY_REFILL_INTERVAL_SEC`) |
| `netrun-orchestrator-validation.service` | proxy validation loop |
| `netrun-orchestrator-watchdog.service` | recovers stuck `running` jobs, releases expired reservations, invalidates stale `pending_validation`, clears expired delivery content; runs pergb cleanup phase 5 (B-8.2) |
| `netrun-orchestrator-traffic-poll.service` | pergb traffic poller (B-8.2) — reads node-agent `/accounting`, writes `traffic_samples`, fires depletion-disable when an account crosses its quota |

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

Traffic-poll tunables (in `.env`, all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `TRAFFIC_POLL_INTERVAL_SEC` | `60` | Loop interval. Clamped to ≥ `TRAFFIC_POLL_MIN_INTERVAL_SEC`. |
| `TRAFFIC_POLL_MIN_INTERVAL_SEC` | `30` | Hard floor for the loop interval. |
| `TRAFFIC_POLL_REQUEST_TIMEOUT_SEC` | `10` | Per-node `/accounting` request timeout. |
| `TRAFFIC_POLL_DEGRADE_AFTER` | `5` | Consecutive failures before flipping `nodes.runtime_status='degraded'`. |

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

**Pay-per-GB endpoints (stubs in B-8.1, full impl in B-8.2):**

- `POST   /v1/orders/reserve_pergb`             (501 until B-8.2)
- `POST   /v1/orders/{order_ref}/topup_pergb`   (501 until B-8.2)
- `GET    /v1/orders/{order_ref}/traffic`       (501 until B-8.2)
- `POST   /v1/admin/traffic/poll`               (501 until B-8.2)

**Legacy aliases (deprecated):** `/health`, `/nodes`, `/nodes/{id}`,
`/jobs`, `/jobs/{id}`, `/jobs/{id}/proxies.list` — same handlers, scheduled
for removal.

All endpoints require `X-NETRUN-API-KEY` matching `ORCHESTRATOR_API_KEY`.

---

## 11. Nginx + TLS (Wave B-7b.5)

By default the orchestrator binds to `127.0.0.1` only — no external
access. For environments that need external access (production behind
a reverse proxy), use the opt-in `scripts/install_nginx.sh`.

### When to install nginx

- You need external access to `/v1/*` endpoints from the bot or
  operators outside the orchestrator host.
- You want per-network ACLs on `/metrics` (the FastAPI process has no
  auth on this endpoint by design).

Skip nginx if all clients run on the orchestrator host (e.g. local
development, single-machine deployments). The default 127.0.0.1 bind
is sufficient.

### Installing the reverse proxy

Run as root on the orchestrator host:

```bash
cd /opt/netrun-orchestrator
bash scripts/install_nginx.sh
```

Defaults:

| Variable | Default | Effect |
|----------|---------|--------|
| `NGINX_LISTEN_PORT` | `8091` | Port nginx listens on. NOT 80 — that is intentionally avoided to coexist with other projects on this host. Run with `NGINX_LISTEN_PORT=443` if you have TLS configured. |
| `NGINX_SERVER_NAME` | `orchestrator.localhost` | Explicit server_name so nginx can route by Host header without becoming a default vhost. |
| `METRICS_ALLOW_NETWORKS` | (empty) | Comma-separated CIDRs allowed to scrape `/metrics` in addition to `127.0.0.1`. Example: `10.0.0.0/8,192.168.1.0/24`. |

Override at install time:

```bash
NGINX_LISTEN_PORT=443 \
NGINX_SERVER_NAME=api.example.com \
METRICS_ALLOW_NETWORKS="10.0.0.0/8" \
  bash scripts/install_nginx.sh
```

The script is idempotent — re-running updates the config and reloads
nginx, never creates duplicates.

### Adding TLS with certbot

Certbot is NOT installed by `install_nginx.sh` — operators run it
manually after pointing DNS at the host:

```bash
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d api.example.com
```

Certbot rewrites `/etc/nginx/sites-available/netrun-orchestrator.conf`
in place to add `listen 443 ssl;` and the cert paths. Re-running
`scripts/install_nginx.sh` afterwards will OVERWRITE these certbot
edits — apply env overrides directly when you next re-install, or
re-run certbot.

### Coexistence with other projects on the same host

The template intentionally:
- Uses an explicit `server_name` (no wildcard / no `default_server`).
- Listens on a non-80 port by default.
- Touches only `/etc/nginx/sites-available/netrun-orchestrator.conf`
  and its symlink in `sites-enabled/`. Does NOT remove existing
  `default` or other vhosts.

Verify your other projects still respond after install:

```bash
nginx -T | grep -E '(server_name|listen)'   # see all vhosts
curl -sI http://other-project.example.com/  # confirm reachable
```

---

## 12. Pay-per-GB operations (Wave B-8.2 + B-8.3)

Pay-per-GB is a second product line on top of per-piece IPv6. Each pergb
buyer gets a dedicated port on a node; nftables per-port counters provide
the byte-accounting source. The orchestrator polls `/accounting` on each
node, writes `traffic_samples`, updates `traffic_accounts.bytes_used`, and
disables the port via `POST /accounts/{port}/disable` when the account
crosses its `bytes_quota`.

### 12.1 End-to-end smoke (against a live test node)

```bash
# 1. Pre-condition: node-agent on host has nftables persistence
#    (install_node.sh — B-8.1) and /describe.supports.accounting == true.

# 2. Reserve a pergb account
curl -sS -X POST http://127.0.0.1:8090/v1/orders/reserve_pergb \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"user_id":1,"sku_id":42,"gb_amount":5}' | jq .

# Response carries port, host, login, password — use those to send traffic.

# 3a. Force-poll instead of waiting 60s (B-8.3):
curl -sS -X POST http://127.0.0.1:8090/v1/admin/traffic/poll \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" | jq .

# 3b. Or wait one polling cycle (~60s default), then check usage:
curl -sS -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  http://127.0.0.1:8090/v1/orders/<order_ref>/traffic | jq .

# 4. Top-up to extend quota + lease:
curl -sS -X POST http://127.0.0.1:8090/v1/orders/<order_ref>/topup_pergb \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"sku_id":42,"gb_amount":10}' | jq .
```

### 12.1.5 Admin force-poll (B-8.3)

`POST /v1/admin/traffic/poll` runs one polling cycle synchronously and
returns the same counters the scheduler logs each tick. Useful for:

- Smoke-testing a fresh deploy without waiting 60s.
- Verifying depletion → disable transitions immediately after triggering
  enough traffic on a port.
- Debugging stuck accounts: scope to one account with `?account_id=N` to
  see exactly what that one account's poll cycle yields.

```bash
# Full cycle
curl -sS -X POST http://127.0.0.1:8090/v1/admin/traffic/poll \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" | jq .

# Scope to one node (UUID from /v1/nodes)
curl -sS -X POST "http://127.0.0.1:8090/v1/admin/traffic/poll?node_id=<UUID>" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" | jq .

# Scope to one account (BIGSERIAL from traffic_accounts.id)
curl -sS -X POST "http://127.0.0.1:8090/v1/admin/traffic/poll?account_id=42" \
  -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" | jq .
```

Response shape:

```json
{
  "accounts_polled": 4,
  "nodes_polled": 2,
  "bytes_observed_total": 1234567,
  "counter_resets_detected": 0,
  "accounts_marked_depleted": 1
}
```

The endpoint shares an in-process `TrafficPollService` instance — its
non-blocking lock prevents two concurrent admin calls from racing each
other, but does NOT prevent racing the standalone scheduler unit
(separate process, separate lock). At 60s cadence cross-process race
is rare; if it happens, the worst case is a single sample double-write
(rejected by the next anchor read since cumulative counters are
monotonic).

### 12.2 Scheduler tuning

The polling worker honors `TRAFFIC_POLL_INTERVAL_SEC` (default 60s,
clamped to ≥ 30s). Lowering it costs node-side CPU on `nft -j list
counters` per cycle; raising it delays detection of quota crossings.
60s is the default lab-validated value.

`TRAFFIC_POLL_DEGRADE_AFTER` controls how many consecutive `/accounting`
failures flip the node's `runtime_status` to `degraded`. Default 5 — at
60s cadence that is 5 minutes of confirmed badness before disabling new
allocations to that node.

### 12.3 Troubleshooting matrix

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `traffic_counter_reset_detected` warnings spike on one node | nftables persistence missing on that node host (counters reset on reboot) | Re-run `bash install_node.sh` on the node — it sets `systemctl enable nftables` and `nft list ruleset > /etc/nftables.conf`. |
| `traffic_poll_partial_response` warnings | node-agent returned 200 with only some of the requested ports populated. Usually transient (race with refill/clean) | No action — missing ports are skipped this cycle and picked up next cycle. |
| Account stuck in `depleted` after a top-up | `post_enable` failed during the top-up reactivation (logged `pergb_account_reactivate_failed`) | Manually call `curl -X POST <NODE_URL>/accounts/<PORT>/enable`. The next poll will see traffic resume. |
| Node `runtime_status='degraded'` after maintenance | 5 consecutive failures during a stop window | Restart the node-agent. The next successful cycle resets the in-process counter; flip back via `UPDATE nodes SET runtime_status='active' WHERE id=...` (or wait for the next refill cycle to re-enroll). |
| `netrun_traffic_poll_lag_sec` rising | scheduler is failing or slow on every cycle | Check `journalctl -u netrun-orchestrator-traffic-poll -n 100` for stack traces; verify `pg_isready`. |
| `netrun_traffic_over_usage_total` growing | account crossed quota in one big delta (slow polling + fast traffic, or recovery after node outage) | Expected behavior per design § 8.2 — accept the small over-billing tail or reduce `TRAFFIC_POLL_INTERVAL_SEC`. |

### 12.4 Inspection queries

```sql
-- Active pergb accounts and their usage
SELECT
  o.order_ref, t.bytes_used, t.bytes_quota,
  ROUND(100.0 * t.bytes_used / NULLIF(t.bytes_quota, 0), 1) AS pct,
  t.last_polled_at, t.expires_at, i.node_id, i.port
FROM traffic_accounts t
JOIN orders o ON o.id = t.order_id
JOIN proxy_inventory i ON i.id = t.inventory_id
WHERE t.status = 'active'
ORDER BY t.last_polled_at NULLS FIRST
LIMIT 50;

-- Recent counter resets (look for clustered node_id)
SELECT account_id, collected_at
FROM traffic_samples
WHERE counter_reset_detected = true
ORDER BY collected_at DESC
LIMIT 20;
```

### 12.5 Admin stats pergb subsection (B-8.3)

`GET /v1/admin/stats` includes a `pergb` block alongside the existing
`sales` / `inventory` / `nodes`:

```bash
curl -sS -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  http://127.0.0.1:8090/v1/admin/stats | jq .pergb
```

```json
{
  "active_accounts": 17,
  "depleted_accounts": 3,
  "expired_accounts": 1,
  "bytes_consumed_7d": 5497558138880,
  "top_skus_by_revenue_7d": [
    {"sku_code": "pergb_us_30gb", "revenue": "184.50", "accounts": 6},
    {"sku_code": "pergb_us_5gb",  "revenue": "60.00",  "accounts": 12}
  ]
}
```

`bytes_consumed_7d` aggregates `bytes_in_delta + bytes_out_delta` from
`traffic_samples` (NOT raw cumulative readings — counter resets would
otherwise inflate). Top-SKU list is capped at 5 rows.

### 12.6 Out of scope (deferred to B-8.4 / D)

- Real-money smoke against a live billing flow (B-8.4 once nodes return to service).
- Hard per-port nftables rate-limit cap (Wave D).
- Cross-SKU top-up (D4.5 rejection — same `sku_id` required).

---

## 13. Production hardening (Wave D)

Three independent operational layers: TLS termination, nightly DB
backups, and metrics dashboards. None of them affect the application
code path — they wrap the existing service.

### 13.1 TLS termination via nginx + Let's Encrypt

The orchestrator now has two nginx integrations. Pick one:

- `scripts/install_nginx.sh` (Wave B-7b.5) — HTTP-only, non-default
  port, fine for single-host or local development.
- `deploy/scripts/install_nginx_tls.sh` (Wave D) — public TLS on 443,
  port 80 redirects, /metrics localhost-only. **Pick this for prod.**

Pre-requisites:

1. A domain you control (example: `orch.netrun.live`).
2. An A-record for that domain pointing at the orchestrator host
   (e.g. `95.217.98.125`). Verify with `dig +short orch.netrun.live`
   from a third machine before running the script — certbot's http-01
   challenge will fail otherwise.
3. Ports 80 and 443 open on the host firewall.
4. Orchestrator bound to `127.0.0.1` only — set
   `ORCHESTRATOR_HOST=127.0.0.1` in `/opt/netrun-orchestrator/.env`
   and restart `netrun-orchestrator`. The systemd unit reads this via
   `python -m orchestrator.server`; no unit edit required.

Install:

```bash
sudo bash deploy/scripts/install_nginx_tls.sh orch.netrun.live ops@netrun.live
```

The script:

1. `apt-get install -y nginx certbot python3-certbot-nginx` (idempotent).
2. Renders `deploy/nginx/orchestrator-tls.conf.template` with the
   domain + `ORCHESTRATOR_PORT` substituted into
   `/etc/nginx/sites-available/netrun-orchestrator-tls.conf`.
3. Symlinks into `sites-enabled/`, runs `nginx -t`.
4. `certbot --nginx -d <domain> --non-interactive --agree-tos -m <email> --redirect`.
5. `systemctl reload nginx`.

Smoke after install:

```bash
curl -sI https://orch.netrun.live/health   # 401 (good — auth wall up)
curl -sI -H "X-NETRUN-API-KEY: $ORCHESTRATOR_API_KEY" \
  https://orch.netrun.live/health          # 200
```

Bot-side: change `ORCHESTRATOR_BASE_URL` in the bot's `.env` to
`https://orch.netrun.live` and restart the bot units.

Renewal: certbot installs its own `certbot.timer` — no extra cron
needed. Verify with `systemctl list-timers | grep certbot`.

Coexistence: the TLS template uses an explicit `server_name` and
does NOT set `default_server`. Other vhosts on the same nginx are
untouched.

### 13.2 Nightly pg_dump backups

Daily snapshot of `netrun_orchestrator`, gzipped, kept for 30 days.

Install:

```bash
sudo bash deploy/scripts/install_auto_backup.sh
```

Layout:

| Path | Purpose |
|------|---------|
| `/usr/local/bin/netrun-auto-backup.sh` | Executable copy of `auto_backup.sh`. |
| `/etc/cron.d/netrun-backup` | `0 3 * * * root …` daily run. |
| `/var/backups/netrun/orchestrator_<DATE>.sql.gz` | Dumps. |
| `/var/log/netrun-backup.log` | stdout+stderr from cron runs. |

Verify a manual run before walking away:

```bash
sudo /usr/local/bin/netrun-auto-backup.sh
ls -lh /var/backups/netrun/
```

#### Restore from a dump

```bash
# Pick a backup file:
ls -lh /var/backups/netrun/

# Stop the orchestrator + workers so no writes race the restore.
sudo systemctl stop netrun-orchestrator netrun-orchestrator-worker \
  netrun-orchestrator-refill netrun-orchestrator-traffic-poll \
  netrun-orchestrator-validation netrun-orchestrator-watchdog

# Drop + recreate (DESTROYS current data — only do this on the
# machine you intend to restore on, never on a healthy primary).
sudo -u postgres psql -c "DROP DATABASE netrun_orchestrator;"
sudo -u postgres psql -c "CREATE DATABASE netrun_orchestrator OWNER netrun_orchestrator;"

# Stream the dump back in.
gunzip -c /var/backups/netrun/orchestrator_20260508_030000.sql.gz \
  | sudo -u postgres psql -d netrun_orchestrator

# Start services back up.
sudo systemctl start netrun-orchestrator netrun-orchestrator-worker \
  netrun-orchestrator-refill netrun-orchestrator-traffic-poll \
  netrun-orchestrator-validation netrun-orchestrator-watchdog
```

Off-site copy (S3 / B2 / rsync) is deferred — for now the dump lives
only on the orchestrator host. Add an off-site sync step before
treating this as full DR.

Tunables (override in environment when running the script):

| Variable | Default | Effect |
|----------|---------|--------|
| `BACKUP_DIR` | `/var/backups/netrun` | Where dumps land. |
| `DB_NAME` | `netrun_orchestrator` | Database name passed to `pg_dump`. |
| `RETENTION_DAYS` | `30` | Dumps older than this are deleted. |

### 13.3 Grafana dashboard

Importable JSON: `deploy/grafana/orchestrator.json`.

Pre-requisite: a Prometheus instance scraping the orchestrator's
`/metrics` endpoint (the TLS template restricts it to localhost, so
Prometheus must run on the same host or be allow-listed). Wiring
Prometheus is outside this repo — a minimal scrape job:

```yaml
scrape_configs:
  - job_name: netrun-orchestrator
    static_configs:
      - targets: ['127.0.0.1:8090']
```

Import the dashboard:

1. Grafana → Dashboards → New → Import.
2. Upload `deploy/grafana/orchestrator.json` (or paste its contents).
3. Pick the Prometheus datasource when prompted.

Panels (Wave D):

- HTTP rate / 5xx rate / p95 latency, broken down by route template.
- Reserve / commit / release rate by status.
- Scheduler runs by (scheduler, status).
- Watchdog actions per action.
- Inventory available per SKU.
- Pergb accounts active vs depleted (stat panel).
- Traffic poll lag (60s yellow / 180s red).
- Billed bytes/s by (sku_code, direction).

If you don't have Grafana yet, the simplest path is the official
package:

```bash
sudo apt-get install -y apt-transport-https software-properties-common
sudo wget -qO /etc/apt/keyrings/grafana.gpg https://apt.grafana.com/gpg.key
echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" \
  | sudo tee /etc/apt/sources.list.d/grafana.list
sudo apt-get update && sudo apt-get install -y grafana
sudo systemctl enable --now grafana-server
# Then visit http://<host>:3000 — admin/admin, change on first login.
```

Do not expose `:3000` to the public internet. Tunnel via SSH or put
it behind nginx auth.

### 13.4 Log paths cheat-sheet (Wave D additions)

| Service | Log |
|---------|-----|
| Cron backup | `/var/log/netrun-backup.log` |
| nginx access | `/var/log/nginx/access.log` |
| nginx error | `/var/log/nginx/error.log` |
| certbot | `/var/log/letsencrypt/letsencrypt.log` |

The orchestrator's own units already go to journald — query with
`journalctl -u netrun-orchestrator -f`.

---

## 14. Pay-per-GB safety net (Wave D)

Goal: when a user's `bytes_used >= bytes_quota`, **the proxy must
actually stop passing traffic on the node**, not just be flagged as
depleted in the database. Without this, the user paid for 1 GB but
keeps receiving unlimited until the next polling cycle (or forever,
if the node missed the disable call).

### 14.1 What's wired (end-to-end)

1. **Polling cycle (`netrun-orchestrator-traffic-poll`)** flips an
   account `active → depleted` when the new sample crosses
   `bytes_quota`. In the same DB transaction it sets `depleted_at`.
2. Immediately after the flip, the polling worker calls
   `POST /accounts/{port}/disable` on the node-agent. The node's
   nftables rule gets a `DROP` for that IPv6 alias.
3. The orchestrator stamps `traffic_accounts.last_block_attempt_at`
   regardless of outcome. On success it also flips
   `node_blocked = TRUE`. On failure (timeout, 5xx, network
   blip) `node_blocked` stays `FALSE`.
4. Every watchdog cycle (60s) **phase 5.4** sweeps:
   ```sql
   SELECT … FROM traffic_accounts
   WHERE status = 'depleted'
     AND node_blocked = FALSE
     AND (last_block_attempt_at IS NULL
          OR last_block_attempt_at < now() - INTERVAL '5 minutes')
   ```
   For each row it retries `post_disable`. Throttle is 5 minutes
   per attempt; batch limit is 100 rows per cycle.
5. On reactivation (`POST /v1/orders/{ref}/topup_pergb` flips the
   account `depleted → active`), `PergbService` calls
   `POST /accounts/{port}/enable` to restore the `ACCEPT` rule.
   Same persistence rules apply via `last_unblock_attempt_at`
   and **phase 5.5** retries.

### 14.2 Node-runtime requirement

The node-agent must implement two endpoints (already shipped in
`netrun-node` per Wave B-8.2 design § 3.2 / § 3.3):

| Path | Method | Behavior |
|------|--------|----------|
| `/accounts/{port}/disable` | `POST` | Idempotent DROP rule install. Already-disabled returns 200. |
| `/accounts/{port}/enable`  | `POST` | Idempotent ACCEPT restore. Already-enabled returns 200. |

If a node doesn't implement these (or implements them buggy), the
orchestrator's safety net cannot help — the watchdog will keep
retrying with `last_block_attempt_at` advancing every 5 minutes,
visible in the structured log under `watchdog_pergb_block_retry_failed`.

### 14.3 Recovery from node restarts

If a node restarts and loses its nftables state, the orchestrator
considers all its currently-depleted accounts as still blocked
(because `node_blocked = TRUE` from the last successful disable).
The next polling cycle will not re-call `post_disable` because the
flip has already happened. **This is the failure mode the watchdog
phase 5.4 protects against** — but only when `node_blocked = FALSE`.

For the case where a node restarts after a successful disable:
either the operator runs a manual reconciliation, or we accept the
small window until the user's lease expires (the watchdog phase 5.1
will flip the account to `expired` at lease end and the inventory
will recycle). In practice node restarts are rare; the more common
case is a transient network blip during the disable RTT, which is
exactly what phase 5.4 fixes.

### 14.4 Apply migration 026

```bash
cd /opt/netrun-orchestrator
sudo -u postgres psql -d netrun_orchestrator -f migrations/026_traffic_accounts_safety_net.sql
sudo systemctl restart netrun-orchestrator netrun-orchestrator-traffic-poll \
                       netrun-orchestrator-watchdog
```

The migration is `IF NOT EXISTS` on every column / index — re-applying
is safe.

### 14.5 Smoke test

The operator script `deploy/scripts/pergb_smoke.sh` walks the entire
flow: discover SKU, reserve 1 GB, push traffic, verify usage,
exhaust quota, assert proxy actually stops.

```bash
ORCHESTRATOR_API_KEY="$(grep ^ORCHESTRATOR_API_KEY /opt/netrun-orchestrator/.env | cut -d= -f2-)" \
  bash /opt/netrun-orchestrator/deploy/scripts/pergb_smoke.sh DE 1
```

Expected exit codes:

| Exit | Meaning |
|------|---------|
| 0    | Quota was enforced (curl failed OR `/traffic` reports `depleted`). |
| 1    | Setup error (missing SKU, missing inventory, missing API key). |
| 2    | **Safety net broken** — proxy still served traffic past 1.1×quota. Investigate. |

If you hit exit code 2, the structured log to look at:

```bash
journalctl -u netrun-orchestrator-traffic-poll -n 100 | grep -E '(traffic_account_disable_failed|traffic_account_depleted)'
journalctl -u netrun-orchestrator-watchdog     -n 100 | grep watchdog_pergb_block_retry
```

### 14.6 Inspecting current state

```sql
-- Accounts where the safety net is still settling.
SELECT id, status, bytes_used, bytes_quota, node_blocked,
       last_block_attempt_at, last_unblock_attempt_at
FROM traffic_accounts
WHERE (status = 'depleted' AND node_blocked = FALSE)
   OR (status = 'active'   AND node_blocked = TRUE)
ORDER BY id;
```

A persistently non-empty result set across multiple watchdog cycles
means a node is wedged refusing disable/enable — investigate the
node-agent logs on the host pointed to by `nodes.url`.
