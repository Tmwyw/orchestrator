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

## 3. Adding nodes

```bash
cd /opt/netrun-orchestrator
bash scripts/add_node.sh <url> [name] [geo] [capacity] [node_api_key] [force]
```

Example:

```bash
bash scripts/add_node.sh https://node-de-1.example.com node-de-1 DE 1000 NODE_KEY_HERE
```

The script POSTs to `/nodes` (no `/v1` prefix — see §9 footnote) and the
orchestrator calls the node's `/health` endpoint before persisting. Pass `true`
as the 6th argument to bypass the health check.

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

## 5. Starting schedulers (refill + validation)

Until Wave B-7 ships dedicated systemd units, the refill scheduler and
validation worker run inside `screen` sessions:

```bash
bash scripts/start_schedulers.sh
```

This spawns:

- `netrun-refill` → `python -m orchestrator.refill_scheduler`
- `netrun-validation` → `python -m orchestrator.validation_scheduler`

Inspect:

```bash
screen -ls                         # both sessions should be listed
screen -r netrun-refill            # attach (Ctrl+A, D to detach)
screen -r netrun-validation
```

Stop:

```bash
bash scripts/stop_schedulers.sh
```

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

---

## 9. Endpoint paths (footnote)

The orchestrator currently exposes two path styles. This will be unified in
Wave B-7.

**Legacy (no prefix):**

- `GET    /health`
- `GET    /nodes`
- `POST   /nodes`
- `DELETE /nodes/{node_id}`
- `POST   /jobs`
- `GET    /jobs/{job_id}`
- `GET    /jobs/{job_id}/proxies.list`

**Sale-domain (`/v1/` prefix):**

- `POST   /v1/orders/reserve`
- `POST   /v1/orders/{order_ref}/commit`
- `POST   /v1/orders/{order_ref}/release`
- `POST   /v1/orders/{order_ref}/extend`
- `GET    /v1/orders/{order_ref}`
- `GET    /v1/orders/{order_ref}/proxies?format=socks5_uri|host_port_user_pass|user_pass_at_host_port|json`

All endpoints require `X-NETRUN-API-KEY` matching `ORCHESTRATOR_API_KEY`.
