# Wave B — Design Document

Version: 1.0
Date: 2026-04-28
Status: APPROVED — implementation may start

This document records the architectural agreement for Wave B of the NETRUN
project: extending `netrun-orchestrator` from a control-plane HTTP API
(nodes + jobs only) into a full sale-domain backend (inventory, orders,
delivery, refill, validation, watchdog, heartbeat, lifecycle, archive,
pay-per-GB).

It is the **source of truth** for Wave B implementation. Any deviation
must update this document first.

---

## 1. Goal and scale targets

### Business goals

- Sell IPv6 proxies to Telegram-bot users.
- Sell pay-per-GB datacenter proxies to the same users.
- Run the entire stack on a Telegram-bot front + orchestrator backend +
  300+ proxy nodes, three independent deployments.

### Scale targets

| Metric | Target |
|---|---|
| Proxy nodes connected | 300+ |
| Active TG-bot users | 20 000+ |
| Concurrent purchases at peak | 1 000 – 2 000 users |
| Proxies per single purchase | 100 – 30 000 |
| Total inventory pool size at peak | 1.5M+ proxies |
| Per-node hardware budget | 4 GB RAM, 2 CPU |
| `POST /v1/orders/reserve` p95 | < 200 ms |
| `POST /v1/orders/reserve` p99 | < 500 ms |
| Sustained throughput per orchestrator instance | 500 RPS |

### Architecture rule

```
                   (HTTP)              (HTTP)
   netrun-tg_bot ─────────► netrun-orchestrator ─────────► node_runtime × 300
   client                   central                         workers
   (Postgres + Redis)       (Postgres + Redis)              (3proxy + nftables)
```

- Bot calls Orchestrator. Orchestrator calls Nodes. Nodes are passive HTTP
  servers, never call Orchestrator.
- No shared filesystem between roles. No Python imports between roles.
- Each role has its own Postgres database and Redis instance.

---

## 2. Tech stack (orchestrator role)

### Existing in repo (kept)

- FastAPI 0.115.x
- httpx (sync) → migrate to async
- psycopg 3.2 (sync) → migrate to **asyncpg**
- uvicorn[standard]

### Added in Wave B-0

- Pydantic v2 — all request/response models
- asyncpg — replaces sync psycopg in API and worker code
- redis.asyncio (redis-py async) — Redis 7+
- pgbouncer — recommended in `install_orchestrator.sh` (transaction-mode)
- ruff (format + lint, strict)
- mypy (strict for new code, gradual for legacy)
- pytest + pytest-asyncio + httpx test-client
- testcontainers (Postgres + Redis for integration tests)

### Production deps additions for Wave B-1+

- structlog — structured logging
- prometheus-client — `/metrics` endpoint

---

## 3. Decisions log

All decisions made during design phase. Frozen.

| ID | Decision | Chosen | Rationale |
|---|---|---|---|
| D1 | Sync vs async API | **Async** (asyncpg + async def handlers) | Required for 500 RPS target |
| D2 | Inventory snapshot in Redis | **Postgres source of truth + Redis sorted-set snapshot** (CDC sync) | Better perf at peak |
| D3 | Delivery storage | **All in DB** (TEXT/JSONB column with TTL=30d on content) | No FS dependency, multi-instance ready |
| D4 | Legacy data migration | **No migration**, fresh schema, legacy DB archived as-is | No production data yet |
| D5 | Bot cutover timing | **After Wave B-7** (full B done, refill stable in prod) | Minimize integration churn |
| Pre-gen | Inventory model | **Pre-generated pool** + on-demand refill loop | Sub-second purchase latency |
| Geo | SKU model | **One SKU = one geo + protocol** (e.g. `ipv6_us_socks5`) | Matches existing UI structure |
| Stock-out UX | When pool empty | **Hard reject** with `available_now` count, no partial fulfillment, no on-demand burst | Simple allocator, predictable UX |
| Distribution | Refill per node | **Equal share** across nodes bound to a SKU | User-requested |
| Distribution | Allocation per node | **Equal share** at order time (1000 across 4 nodes = 250+250+250+250, fallback if uneven pool) | User-requested |
| Lifecycle | Pool TTL | **Infinite while in pool** (until validation fails) | User-requested |
| Lifecycle | Sold TTL | **30 days** by default (per-SKU `duration_days`) | User-requested |
| Lifecycle | Expiring notifications | At -3d / -2d / -1d, **bot polls orchestrator**, notifies user | User-requested |
| Lifecycle | Renewal | Two operations: extend whole order, extend by inventory_ids OR by geo filter | User-requested |
| Lifecycle | Post-expire grace | **3 days** after `expires_at`, then move to `archived` | User-requested |
| Lifecycle | Archive | Records remain in DB with `status=archived`, admin-only export | User-requested |
| Pay-per-GB | Multi-user model | **Variant α: per-user dedicated port** (no shared port, simpler billing) | nftables accounting already works, isolation, simpler |
| Performance | Per-node cap | `capacity = 5000`, `max_parallel_jobs = 1`, `max_batch_size = 1500` | Fits 4 GB / 2 CPU node hardware |
| UI counter freshness | Bot caches SKU list | **30 sec** cache + exact validation only at `POST /reserve` | Realistic UX, low orchestrator load |

---

## 4. Schema

Schema for `netrun_orchestrator` Postgres database. Will be applied via
sequential `migrations/NNN_*.sql` files run by `orchestrator/migrate.py`.

Indexes are listed below each table only when load-critical; trivial PK/FK
indexes are implicit.

### 4.1. Existing tables (extended in Wave B-1)

```sql
-- migrations/003_extend_nodes.sql
ALTER TABLE nodes ADD COLUMN weight INT NOT NULL DEFAULT 100;
ALTER TABLE nodes ADD COLUMN max_parallel_jobs INT NOT NULL DEFAULT 1;
ALTER TABLE nodes ADD COLUMN max_batch_size INT NOT NULL DEFAULT 1500;
ALTER TABLE nodes ADD COLUMN runtime_status TEXT NOT NULL DEFAULT 'active'
  CHECK (runtime_status IN ('active', 'degraded', 'offline', 'disabled'));
ALTER TABLE nodes ADD COLUMN heartbeat_failures INT NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN last_heartbeat_at TIMESTAMPTZ;
ALTER TABLE nodes ADD COLUMN generator_script TEXT;
ALTER TABLE nodes ADD COLUMN generator_args_template JSONB NOT NULL DEFAULT '[]';
ALTER TABLE nodes ADD COLUMN metadata JSONB NOT NULL DEFAULT '{}';

CREATE INDEX idx_nodes_runtime_status ON nodes(runtime_status)
  WHERE runtime_status IN ('active', 'degraded');

-- migrations/004_extend_jobs.sql
ALTER TABLE jobs ADD COLUMN sku_id BIGINT;  -- FK added after skus table
ALTER TABLE jobs ADD COLUMN reason TEXT NOT NULL DEFAULT 'manual'
  CHECK (reason IN ('manual', 'refill', 'api', 'admin'));
ALTER TABLE jobs ADD COLUMN priority INT NOT NULL DEFAULT 10;
ALTER TABLE jobs ADD COLUMN attempts INT NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN max_attempts INT NOT NULL DEFAULT 5;
ALTER TABLE jobs ADD COLUMN payload JSONB NOT NULL DEFAULT '{}';
ALTER TABLE jobs ADD COLUMN locked_by TEXT;
ALTER TABLE jobs ADD COLUMN locked_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN available_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- result_path becomes deprecated (delivery files now in DB)
-- but we keep the column for backward compat with existing /jobs/{id}/proxies.list
```

### 4.2. New tables — sale-domain core (Wave B-1)

```sql
-- migrations/005_skus.sql
CREATE TABLE skus (
  id              BIGSERIAL PRIMARY KEY,
  code            TEXT NOT NULL UNIQUE,                    -- 'ipv6_us_socks5'
  product_kind    TEXT NOT NULL                            -- 'ipv6' | 'datacenter_pergb'
                  CHECK (product_kind IN ('ipv6', 'datacenter_pergb')),
  geo_code        TEXT NOT NULL,                           -- 'US', 'UK', 'DE', ...
  protocol        TEXT NOT NULL                            -- 'socks5' | 'http'
                  CHECK (protocol IN ('socks5', 'http')),
  duration_days   INT NOT NULL DEFAULT 30,                 -- ipv6 only
  price_per_piece NUMERIC(10, 2),                          -- ipv6 only
  price_per_gb    NUMERIC(10, 2),                          -- datacenter_pergb only (filled in B-8)
  target_stock    INT NOT NULL DEFAULT 0,
  refill_batch_size INT NOT NULL DEFAULT 500,
  validation_require_ipv6 BOOLEAN NOT NULL DEFAULT TRUE,
  is_active       BOOLEAN NOT NULL DEFAULT TRUE,
  metadata        JSONB NOT NULL DEFAULT '{}',
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_skus_active_kind ON skus(product_kind, geo_code)
  WHERE is_active = TRUE;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_sku
  FOREIGN KEY (sku_id) REFERENCES skus(id) ON DELETE SET NULL;
```

```sql
-- migrations/006_sku_node_bindings.sql
CREATE TABLE sku_node_bindings (
  id             BIGSERIAL PRIMARY KEY,
  sku_id         BIGINT NOT NULL REFERENCES skus(id) ON DELETE CASCADE,
  node_id        TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  weight         INT NOT NULL DEFAULT 100,
  max_batch_size INT NOT NULL DEFAULT 1500,
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(sku_id, node_id)
);

CREATE INDEX idx_bindings_sku_active ON sku_node_bindings(sku_id)
  WHERE is_active = TRUE;
```

```sql
-- migrations/007_node_port_allocations.sql
CREATE TABLE node_port_allocations (
  id           BIGSERIAL PRIMARY KEY,
  job_id       TEXT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
  node_id      TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  start_port   INT NOT NULL,
  end_port     INT NOT NULL,
  proxy_count  INT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'reserved'
               CHECK (status IN ('reserved', 'released')),
  released_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_port_alloc_node_status ON node_port_allocations(node_id, status)
  WHERE status = 'reserved';
```

```sql
-- migrations/008_proxy_inventory.sql
CREATE TABLE proxy_inventory (
  id                    BIGSERIAL PRIMARY KEY,
  sku_id                BIGINT NOT NULL REFERENCES skus(id) ON DELETE CASCADE,
  node_id               TEXT NOT NULL REFERENCES nodes(id) ON DELETE RESTRICT,
  generation_job_id     TEXT REFERENCES jobs(id) ON DELETE SET NULL,
  login                 TEXT NOT NULL,
  password              TEXT NOT NULL,
  host                  TEXT NOT NULL,
  port                  INT NOT NULL,
  status                TEXT NOT NULL DEFAULT 'pending_validation'
                        CHECK (status IN (
                          'pending_validation',
                          'available',
                          'reserved',
                          'sold',
                          'expired_grace',
                          'archived',
                          'invalid'
                        )),
  reservation_key       TEXT,
  reserved_at           TIMESTAMPTZ,
  order_id              BIGINT,                            -- FK added after orders table
  sold_at               TIMESTAMPTZ,
  expires_at            TIMESTAMPTZ,                       -- NULL while in pool, NOT NULL after sold
  archived_at           TIMESTAMPTZ,
  external_ip           TEXT,
  geo_country           TEXT,
  geo_city              TEXT,
  latency_ms            INT,
  ipv6_only             BOOLEAN,
  dns_sanity            BOOLEAN,
  validation_error      TEXT,
  validated_at          TIMESTAMPTZ,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Critical performance indexes
CREATE INDEX idx_inventory_pool ON proxy_inventory(sku_id, node_id, status)
  WHERE status = 'available';

CREATE INDEX idx_inventory_pending ON proxy_inventory(sku_id, status)
  WHERE status = 'pending_validation';

CREATE INDEX idx_inventory_reserved ON proxy_inventory(reservation_key)
  WHERE reservation_key IS NOT NULL;

CREATE INDEX idx_inventory_expires ON proxy_inventory(expires_at)
  WHERE status IN ('sold', 'expired_grace');

CREATE INDEX idx_inventory_order ON proxy_inventory(order_id)
  WHERE order_id IS NOT NULL;
```

```sql
-- migrations/009_orders.sql
CREATE TABLE orders (
  id                BIGSERIAL PRIMARY KEY,
  order_ref         TEXT NOT NULL UNIQUE,                  -- public reference
  user_id           BIGINT NOT NULL,
  sku_id            BIGINT NOT NULL REFERENCES skus(id),
  status            TEXT NOT NULL                          -- 'reserved' | 'committed' | 'released' | 'expired'
                    CHECK (status IN ('reserved', 'committed', 'released', 'expired')),
  requested_count   INT NOT NULL,
  allocated_count   INT NOT NULL DEFAULT 0,
  reservation_key   TEXT NOT NULL,
  reserved_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at        TIMESTAMPTZ NOT NULL,                  -- reservation TTL boundary
  committed_at      TIMESTAMPTZ,
  released_at       TIMESTAMPTZ,
  proxies_expires_at TIMESTAMPTZ,                          -- when committed: now() + sku.duration_days
  price_amount      NUMERIC(18, 8),
  idempotency_key   TEXT UNIQUE,
  metadata          JSONB NOT NULL DEFAULT '{}',
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_orders_user_created ON orders(user_id, created_at DESC);
CREATE INDEX idx_orders_reserved_expires ON orders(expires_at) WHERE status = 'reserved';
CREATE INDEX idx_orders_committed_expiring ON orders(proxies_expires_at) WHERE status = 'committed';

ALTER TABLE proxy_inventory ADD CONSTRAINT fk_inventory_order
  FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE SET NULL;
```

```sql
-- migrations/010_delivery_files.sql
CREATE TABLE delivery_files (
  id                BIGSERIAL PRIMARY KEY,
  order_id          BIGINT NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
  format            TEXT NOT NULL                          -- 'socks5_uri' | 'host_port_user_pass' | 'user_pass_at_host_port' | 'json'
                    CHECK (format IN ('socks5_uri', 'host_port_user_pass', 'user_pass_at_host_port', 'json')),
  line_count        INT NOT NULL,
  checksum_sha256   TEXT NOT NULL,
  content           TEXT,                                  -- the file body, NULL after content_expires_at
  content_expires_at TIMESTAMPTZ NOT NULL,                 -- now() + 30d, content nulled after
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_delivery_content_expires ON delivery_files(content_expires_at)
  WHERE content IS NOT NULL;
```

### 4.3. Wave B-8 — pay-per-GB tables (designed, applied later)

```sql
-- migrations/020_traffic_accounts.sql
CREATE TABLE traffic_accounts (
  id              BIGSERIAL PRIMARY KEY,
  order_id        BIGINT NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
  inventory_id    BIGINT NOT NULL UNIQUE REFERENCES proxy_inventory(id) ON DELETE CASCADE,
  bytes_quota     BIGINT NOT NULL,                        -- e.g. 10 * 1024 * 1024 * 1024 for 10 GB
  bytes_used      BIGINT NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'depleted', 'expired', 'archived')),
  last_polled_at  TIMESTAMPTZ,
  depleted_at     TIMESTAMPTZ,
  expires_at      TIMESTAMPTZ NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_traffic_active_polling ON traffic_accounts(last_polled_at)
  WHERE status = 'active';

CREATE INDEX idx_traffic_expiring ON traffic_accounts(expires_at)
  WHERE status = 'active';
```

```sql
-- migrations/021_traffic_samples.sql
CREATE TABLE traffic_samples (
  id              BIGSERIAL PRIMARY KEY,
  account_id      BIGINT NOT NULL REFERENCES traffic_accounts(id) ON DELETE CASCADE,
  bytes_in        BIGINT NOT NULL,
  bytes_out       BIGINT NOT NULL,
  collected_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_samples_account ON traffic_samples(account_id, collected_at DESC);
-- Retention: pg_cron / app-side cleanup, keep last 30 days only.
```

---

## 5. Redis strategy

Redis is **complementary** to Postgres. Postgres is source of truth.
Redis is hot-path cache + reservation TTL + distributed locks.

### 5.1. Reservation TTL

```
KEY:    reservation:{order_ref}
TYPE:   STRING (JSON)
VALUE:  {"order_id": 123, "sku_id": 5, "inventory_ids": [...], "user_id": 999}
TTL:    300 sec (default, configurable per request, 30..3600)
```

Set on `POST /v1/orders/reserve`, deleted on `commit` or `release`. On TTL
expiry, a background scanner releases inventory back to `available`.

Note: the underlying `proxy_inventory.reservation_key` and `reserved_at`
columns mirror the Redis state. Redis is the **fast path**; Postgres is
the **persistent guarantee** in case Redis crashes.

### 5.2. Inventory snapshot (D2 = α)

```
KEY:    inv:available:{sku_id}:{node_id}
TYPE:   sorted set, score = inventory_id
TTL:    no TTL, sync from Postgres
```

Used by allocator to pick N proxies per node without touching PG. Synced
from Postgres via:

- Initial load on orchestrator boot.
- Background sync worker every 5 sec — diff PG vs Redis (cheap
  `SELECT id FROM proxy_inventory WHERE updated_at > $last_sync` per SKU).
- Optional: Postgres `LISTEN/NOTIFY` for event-driven invalidation.

```
KEY:    inv:counter:{sku_id}
TYPE:   STRING (number)
TTL:    5 sec
```

Cached counter for `GET /v1/skus` UI display. Bot polls every 30 sec, so
5-sec PG → Redis sync is overkill but cheap.

### 5.3. Distributed locks

```
KEY:    refill:lock:{sku_id}
TYPE:   STRING
VALUE:  worker_id
TTL:    60 sec (configurable per refill cycle estimate)
```

`SET key value NX EX 60` pattern. Only one worker enters refill for the
SKU at a time. Used in refill worker only.

### 5.4. Rate limit

```
KEY:    rl:{user_id}:reserve
TYPE:   STRING (counter)
TTL:    60 sec
```

`INCR + EXPIRE` pattern. Default: 10 reserve req/min per user.

### 5.5. Idempotency cache

```
KEY:    idem:{key}
TYPE:   STRING (JSON)
VALUE:  cached HTTP response
TTL:    24 hours
```

In addition to Postgres `UNIQUE` constraint on `idempotency_key`, Redis
caches the full response for fast retry-replay.

### 5.6. Out of scope for Redis

- User balances → bot Postgres
- Orders history → orchestrator Postgres (paginated)
- Pub/sub for node events → not in Wave B; Wave D maybe

---

## 6. API contract

All endpoints under `/v1/*`. All require header `X-NETRUN-API-KEY`.
Errors follow RFC 7807 Problem Details:
`{"type": "...", "title": "...", "status": 400, "detail": "...", "instance": "/v1/..."}`.

### 6.1. Health and ops

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | DB+Redis ping |
| GET | `/metrics` | Prometheus exposition (Wave B-7) |
| GET | `/ready` | Liveness for systemd |

### 6.2. Nodes (existing, extended)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/nodes` | list nodes, filter `?geo&status` |
| POST | `/v1/nodes` | upsert node |
| POST | `/v1/nodes/{id}/enroll` | self-describe + auto-fill (Wave B-6) |
| GET | `/v1/nodes/{id}/health` | proxy /health to node |
| DELETE | `/v1/nodes/{id}` | soft-delete (`is_active=false`) |
| GET | `/v1/nodes/{id}/events` | recent events for this node |

### 6.3. SKUs

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/skus` | list active SKUs with `available` counters |
| POST | `/v1/skus` | create |
| PATCH | `/v1/skus/{id}` | edit `target_stock`, `price_*`, `is_active` |
| GET | `/v1/skus/{id}` | detail |
| DELETE | `/v1/skus/{id}` | soft-delete |
| GET | `/v1/skus/{id}/bindings` | list bound nodes |
| POST | `/v1/skus/{id}/bindings` | bind nodes |
| DELETE | `/v1/skus/{id}/bindings/{node_id}` | unbind |

### 6.4. Inventory

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/inventory?sku_id=...` | counter only (cached 5s) |
| GET | `/v1/inventory/breakdown?sku_id=...` | per-node breakdown (admin) |

### 6.5. Orders (sale-domain core)

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/orders/reserve` | atomic reserve N proxies, equal across nodes |
| POST | `/v1/orders/{ref}/commit` | finalize after bot debit (status → committed, expires_at set) |
| POST | `/v1/orders/{ref}/release` | undo reserve (inventory back to available) |
| GET | `/v1/orders/{ref}` | get order |
| GET | `/v1/orders/{ref}/proxies?format=...` | stream proxies file (4 formats) |
| POST | `/v1/orders/{ref}/extend` | extend whole order or by `inventory_ids[]` / `geo_code` |
| GET | `/v1/orders/expiring?days_ahead=3,2,1` | bot polls hourly to send notifications |
| POST | `/v1/orders/{ref}/replace` | manual replace with reason |

### 6.6. Refill

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/refill/trigger` | manual refill (admin), specify `sku_id` |
| GET | `/v1/refill/status?sku_id=` | current refill state for SKU |
| GET | `/v1/refill/jobs?sku_id=` | refill job history |

### 6.7. Admin (archive, reports)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/admin/archive?from=&to=&geo=&format=` | export archived proxies |
| GET | `/v1/admin/stats?range=` | sales / inventory / nodes summary |
| GET | `/v1/admin/orders?user_id=&status=` | orders search |

### 6.8. Pay-per-GB (Wave B-8)

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/orders/reserve_pergb` | reserve N×bytes_quota, allocates per-user ports |
| GET | `/v1/orders/{ref}/traffic` | current bytes_used / bytes_quota |
| POST | `/v1/admin/traffic/poll` | manual force-poll all nodes |

### 6.9. Pydantic v2 contracts (illustrative)

```python
class ReserveRequest(BaseModel):
    user_id: int
    sku_id: int
    quantity: int = Field(ge=1, le=50_000)
    reservation_ttl_sec: int = Field(default=300, ge=30, le=3600)
    idempotency_key: str | None = Field(default=None, max_length=128)

class ReserveResponse(BaseModel):
    success: bool
    order_ref: str
    expires_at: datetime
    proxies_count: int
    proxies_url: str  # /v1/orders/{ref}/proxies (download after commit)

class CommitRequest(BaseModel):
    debit_confirmation: str  # bot signs the commit, e.g. user_id+amount hash
    duration_days: int | None = None  # override SKU default
```

### 6.10. Decimal serialization convention (Wave B-7b.3)

All money-typed fields use `Decimal` in Python and `NUMERIC(18,8)` in
Postgres. When Pydantic v2 serializes via `model_dump(mode="json")`,
Decimal becomes a **string** (not a JSON number) to preserve precision.

Affected fields across the API: `revenue` (admin/stats), `price_amount`
(orders), `price_per_piece` and `price_per_gb` (skus). Bot client (Wave C)
MUST parse these as `Decimal(str_value)`, never `float(value)`.

Example response fragment:

```json
{"sales": {"orders": 5, "proxies": 25, "revenue": "12.50000000"}}
```

Floats lose precision past 7-15 digits; user balances stored as Decimal
accumulate rounding errors if money math goes through float. This
convention is enforced at the Pydantic schema layer, not at runtime.

---

## 7. Lifecycle of a proxy

```
                                                    ┌─ invalid (validation failed)
                                                    │
  generated → pending_validation ──validation──► ───┼─ available (in pool)
                                                                │
                                                              reserve
                                                                │
                                                            reserved ──release/expire──► available
                                                                │
                                                              commit
                                                                │
                                                              sold (expires_at = now() + 30d)
                                                                │
                                                          expires_at − 3d
                                                                │
                                                          notify user (-3d, -2d, -1d)
                                                                │
                                                            expires_at
                                                                │
                                                          expired_grace (3 days more)
                                                                │
                                                       (extend?) ─Y─► sold (expires_at += 30d)
                                                                │
                                                                N
                                                                │
                                                            archived (admin-only)
```

### 7.1. Allocator algorithm (D2 = α implementation)

```
POST /v1/orders/reserve {sku_id, quantity, user_id}:
  1. Load active sku_node_bindings for sku_id (only nodes runtime_status='active'
     and bindings.is_active=true).
  2. quota_per_node = quantity // node_count, remainder distributed by binding.weight.
  3. For each node, pick `quota` inventory_ids from Redis sorted set
     `inv:available:{sku_id}:{node_id}` (atomic ZPOPMIN).
  4. If any node returns less than its quota:
     a. Add deficit to a "shortage" bucket.
     b. Re-distribute shortage equally across nodes that still have free capacity.
     c. Repeat 1 pass max — if still short, abort and return all popped IDs.
  5. If still short → 400 insufficient_stock {available_now: X}.
  6. Bulk SQL: `UPDATE proxy_inventory SET status='reserved', reservation_key=...
     WHERE id IN (...) RETURNING *`.
  7. INSERT into orders (status='reserved', reservation_key, expires_at = now() + ttl).
  8. Redis: SET reservation:{order_ref} ex ttl_sec.
  9. Return ReserveResponse.

On TTL expire (background scanner every 30s):
  SELECT order_ref FROM orders WHERE status='reserved' AND expires_at < now();
  For each: same logic as POST /release.
```

### 7.2. Equal distribution invariant

For order of 1000 across 4 nodes with 5000+ available each:
- ideal: 250 + 250 + 250 + 250
- if node-3 has only 100: 250 + 250 + 100 + 250 + (re-distribute 150 across others) = 300 + 300 + 100 + 300 = 1000 ✓

For order of 1000 across 4 nodes with 200 available each (total 800):
- max possible: 200 + 200 + 200 + 200 = 800 < 1000 → **abort with 400, available_now=800**.

### 7.3. Renewal (extend)

```
POST /v1/orders/{ref}/extend
Body: {duration_days: 30}  # whole order
or:   {inventory_ids: [123, 456], duration_days: 30}  # subset
or:   {geo_code: 'US', duration_days: 30}  # by geo

Bot calculates price (qty × price_per_piece × duration_factor),
debits user balance atomically,
then calls extend.

Server:
  IF whole order:
    UPDATE proxy_inventory SET expires_at = expires_at + duration_days
    WHERE order_id = (SELECT id FROM orders WHERE order_ref = $1)
      AND status IN ('sold', 'expired_grace');
    UPDATE orders SET proxies_expires_at = max(...) WHERE order_ref = $1;
  IF inventory_ids:
    same with WHERE id IN (...)
  IF geo_code:
    join via skus.geo_code
```

### 7.4. Archive job (background)

```
Every 1 hour:
  UPDATE proxy_inventory
  SET status='archived', archived_at=now(), order_id=NULL
  WHERE status IN ('sold', 'expired_grace')
    AND expires_at < now() - interval '3 days';

  UPDATE delivery_files
  SET content = NULL
  WHERE content_expires_at < now() AND content IS NOT NULL;
```

### 7.5. Refill engine

```
Every PROXY_REFILL_INTERVAL_SEC (default 30):
  For each active sku:
    available = COUNT(proxy_inventory WHERE sku_id=X AND status='available')
    if available >= target_stock: skip
    deficit = target_stock - available
    to_schedule = min(deficit, refill_batch_size)
    
    bindings = list bindings for sku (active only, equal-split)
    distribution = equal share of to_schedule across bindings.length nodes
    
    for (node_id, qty) in distribution:
      check existing in-flight job: COUNT(jobs WHERE sku_id=X AND node_id=Y AND status IN ('queued','running'))
      if in-flight >= node.max_parallel_jobs: skip
      
      payload = build_refill_payload(sku, node, qty)  # PRODUCTION_PROFILE injected
      INSERT INTO jobs (sku_id, node_id, status='queued', count=qty, reason='refill', payload, ...)
      
      Acquire node-port range (via node_port_allocations + advisory lock or SELECT FOR UPDATE on
      max(end_port) WHERE node_id=Y).
```

### 7.6. Generation worker (claim → run → import)

```
Forever:
  job = claim_next() via SELECT FOR UPDATE SKIP LOCKED LIMIT 1
  if not job: sleep(2)
  
  Process:
    HTTP POST /generate to node (httpx async, timeout = NODE_REQUEST_TIMEOUT_SEC, default 1200s)
    On 5xx / network error: classify_failure → transient → retry; terminal → fail.
    
    On success:
      bulk INSERT into proxy_inventory (status='pending_validation', sku_id, node_id, generation_job_id, ...)
      UPDATE jobs SET status='success', updated_at=now() WHERE id=X
      INSERT INTO job_events (job_id, event='completed', data=...)
```

### 7.7. Validation worker

```
Forever:
  rows = claim_pending_batch (SELECT FOR UPDATE SKIP LOCKED LIMIT 50)
  For each row in parallel (asyncio.gather with semaphore=20):
    SOCKS5/HTTP probe → external_ip + latency
    Optional: validation_require_ipv6 → check IPv6
    Optional: ipapi.co → geo_country/geo_city
  
  Bulk UPDATE: rows with success → status='available'
  Bulk UPDATE: rows with fail → status='invalid', validation_error
  
  Push to Redis: ZADD inv:available:{sku_id}:{node_id} for newly-available rows
```

### 7.8. Heartbeat worker

```
Every PROXY_NODE_HEARTBEAT_INTERVAL_SEC (default 60):
  For each node where runtime_status != 'disabled':
    GET /health → success+status='ready'+ipv6.ok=true → 'active'
                                                       → 'degraded' if ipv6 or duplicates fail
                  fail → heartbeat_failures++
                       → if failures >= 3: 'offline'
```

### 7.9. Watchdog worker

```
Every PROXY_WATCHDOG_INTERVAL_SEC (default 60):
  jobs queued > QUEUED_TIMEOUT (300s) → fail with reason='queued_timeout'
  jobs running > RUNNING_TIMEOUT (1800s) → retry (if attempts < max) or fail
  reservations: orders.status='reserved' AND expires_at < now() → release
  delivery_files: content_expires_at < now() → null content
  archive: see 7.4
```

---

## 8. Pay-per-GB design (Wave B-8)

### 8.1. Architecture (Variant α)

Each pay-per-GB user gets a **dedicated port** on a node. The node's
`proxyyy_automated.sh` already creates per-port nftables counters
(`proxy_${port}_in`, `proxy_${port}_out`, `proxy_${port}_in6`).

We **don't share ports** between users. Pool size for pay-per-GB SKU
equals the maximum concurrent buyers, not the maximum concurrent users.

### 8.2. New node-agent endpoint (in `node_runtime_repo`)

```
GET /accounting?ports=32001,32002,32003
→ {
    "32001": {"bytes_in": 102400, "bytes_out": 5242880, "bytes_in6": 1024},
    "32002": {"bytes_in": ..., ...},
    ...
  }

Implementation: `nft -j list counters table inet proxy_accounting`,
parse JSON output, return per-port aggregates.
```

### 8.3. Orchestrator polling worker

```
Every TRAFFIC_POLL_INTERVAL_SEC (default 60):
  For each active traffic_account:
    Group by node, batch-poll node /accounting?ports=...
    For each (account, sample):
      INSERT INTO traffic_samples (account_id, bytes_in, bytes_out)
      UPDATE traffic_accounts SET bytes_used = sample_total, last_polled_at=now()
      
      IF bytes_used >= bytes_quota:
        Mark status='depleted', depleted_at=now()
        POST /accounts/{port}/disable → node-agent disables 3proxy port
        Notify user (via bot polling /v1/orders/expiring-or-depleted)
```

### 8.4. New node-agent endpoint for disable

```
POST /accounts/{port}/disable
→ kill 3proxy instance for that port (or mark connection-block in nftables)
→ keep port reserved (so it doesn't get reused), but no traffic flows
```

### 8.5. Tier pricing

`skus.metadata` JSONB stores tier table:
```json
{
  "tiers": [
    {"gb_min": 1, "gb_max": 1, "price_per_gb": 1.20},
    {"gb_min": 3, "gb_max": 3, "price_per_gb": 1.10},
    {"gb_min": 5, "gb_max": 5, "price_per_gb": 1.00},
    {"gb_min": 10, "gb_max": 10, "price_per_gb": 0.95},
    {"gb_min": 20, "gb_max": 20, "price_per_gb": 0.85},
    {"gb_min": 30, "gb_max": 30, "price_per_gb": 0.80}
  ]
}
```

Bot picks tier on user choice, calculates price = `gb × tier.price_per_gb`.

### 8.6. Wave B-8 dependencies

- node_runtime patch: add `/accounting` and `/accounts/{port}/disable` endpoints
  → must be coordinated with `Tmwyw/node_runtime` repo
- orchestrator: traffic polling worker, new endpoints, new tables

---

## 9. Performance plan (Wave B-5)

### 9.1. Targets recap

| Metric | Target |
|---|---|
| `POST /v1/orders/reserve` p95 | < 200 ms |
| `POST /v1/orders/reserve` p99 | < 500 ms |
| Sustained RPS per orchestrator instance | 500 |
| `pg_stat_activity` connections at peak | < 100 (via pgbouncer) |
| Redis ops/sec at peak | < 50 000 |
| Successful reservations / total | > 99.5% |
| Reservation expiry rate (user didn't commit) | < 5% |

### 9.2. Load test approach

- Tool: k6 (preferred over locust for HTTP-only scenarios)
- Stages: ramp 0→100 RPS over 1 min, hold 100 RPS for 5 min, ramp to 500 RPS,
  hold 500 RPS for 10 min
- Two orchestrator instances behind nginx round-robin
- One Redis, one Postgres (with pgbouncer)
- Mock node responses (HTTP server with configurable delay)

### 9.3. Tuning knobs

- pgbouncer pool sizes: `default_pool_size=25`, `max_client_conn=10000`
- asyncpg pool: `min_size=10, max_size=50` per instance
- Redis: maxclients=10000, tcp-backlog=511
- Worker count: 4 generation, 8 validation, 2 watchdog, 1 heartbeat
- FastAPI uvicorn workers: 4 per instance

---

## 10. Migration plan — Wave-шаги

| Wave | Title | Estimate | Output |
|---|---|---|---|
| **B-0** | Toolchain + async refactor | 1 week | pyproject.toml, ruff, mypy, pytest, asyncpg, Pydantic v2, all existing endpoints async-rewritten |
| **B-1** | Schema migrations 003-010 | 1-2 weeks | All sale-domain tables created; jobs/nodes extended |
| **B-2** | Refill engine + workers | 1-2 weeks | RefillService + GenerationWorker fully working with new schema |
| **B-3** | Validation pipeline | 1 week | ValidationWorker + node-agent integration |
| **B-4** | Allocator + orders + delivery | 2 weeks | reserve/commit/release/extend/replace + delivery files in DB |
| **B-5** | Performance hardening + load tests | 1 week | k6 scripts, pgbouncer setup, tuning |
| **B-6** | Node enrollment CLI | 3-4 days | enroll-node + verify-node + GET /describe in node_runtime |
| **B-7** | Admin endpoints + observability | 1 week | archive export, stats, /metrics, structlog, alerts |
| **B-8** | Pay-per-GB billing | 2-3 weeks | traffic_accounts, polling worker, node-agent /accounting endpoint, tier pricing |

**Total Wave B: 10-13 weeks** at 2-3 hours/day pace.

After Wave B-7 stable in prod → Wave C (bot integration with HTTP client).
After Wave B-8 stable → Wave C extended (bot supports pay-per-GB).

---

## 11. Open items / future Waves

These are explicitly out of Wave B scope. Recorded for future reference.

| Item | Where addressed |
|---|---|
| Bot HTTP client (`tg_bot/services/orchestrator_client.py`) | Wave C |
| Bot lifecycle of `tg_bot/app/proxy_user/checkout.py` rewrite | Wave C |
| Webhook events (orchestrator → bot push) | Wave D (deferred) |
| Multi-instance Postgres (read replicas) | post-Wave D, only if needed |
| Node self-registration (vs operator-pull enroll) | not planned, security risk |
| Per-piece TTL ≠ 30 days variant | configurable per SKU since B-1 |
| Decimal in money math | balance/amount: `NUMERIC(18,8)` already in schema; bot side in PR #1.6 |

---

## 12. Glossary

- **SKU** — sale unit, one geo + one protocol + one product_kind. Has its
  own pool, target_stock, bindings, price.
- **Pool** — set of `proxy_inventory` rows with `status='available'` for a
  given SKU. Refill maintains it at `target_stock`.
- **Binding** — `sku_node_bindings` row, allows node to host inventory for
  this SKU.
- **Reservation** — temporary lock on inventory rows during `reserve→commit`
  window. Backed by Redis TTL + Postgres `reservation_key`.
- **Order** — committed reservation, has `expires_at` (sold proxies' validity).
- **Archive** — `proxy_inventory.status='archived'`. Historical record only.
- **Production profile** — fingerprint/network/IPv6 contract enforced by
  orchestrator on every job to nodes (`shared/contracts.py:PRODUCTION_PROFILE`).

---

## Sign-off

This document was finalized on 2026-04-28 after agreement on:
- Decisions D1-D5
- All B-0 .. B-8 wave scopes
- Equal-distribution invariants
- 30-day sold TTL + 3-day grace + archive
- pay-per-GB variant α (per-user port)
- 4 GB / 2 CPU node hardware constraints

Implementation starts with Wave B-0 prompt to be issued separately.

---

## Known issues from Wave B-2/B-3/B-4a/B-5b (deferred to later Waves)

| # | Issue | Where | Defer to |
|---|---|---|---|
| 1 | `deficit = target - available` без учёта queued/running → может cause overshoot если генерация медленная | `orchestrator/refill.py:_get_sku_projection` | Wave B-7 (watchdog скорректирует) |
| 2 | RefillService.run_once() — все enqueue в одной транзакции; exception на N-м SKU откатывает 1..N-1 | `orchestrator/refill.py:run_once` | **FIXED in B-7a**: watchdog подхватит unfinished refill-jobs (stuck running → failed; expired reservations → released) |
| 3 | `bulk_insert_inventory_pending` через executemany — N round-trips к БД, медленно на batch=1500 | `orchestrator/jobs.py:bulk_insert_inventory_pending` | Wave B-5 (perf, переход на execute_values/COPY) |
| 4 | `bulk_insert` и `mark_success` — отдельные транзакции; при сбое между ними inventory pending + job running | `orchestrator/worker.py:process_refill_job` | Wave B-7 (watchdog подхватит stuck running) |
| 5 | `verify=False` в `_probe_http_proxy` отключает SSL verify | `orchestrator/validation.py:128` | **FIXED in B-7b.4**: `VALIDATION_STRICT_SSL` config flag gates SSL verify. Default `true` (secure-by-default). |
| 6 | Двойная конверсия `b"...".decode("ascii").encode("idna")` — косметика | `orchestrator/validation.py:176` | косметика, можно фиксить попутно |
| 7 | reserve не атомарен: _sync_claim_per_node_with_rollback (commit) → _sync_insert_order (commit) — 2 transactions, race возможен | `orchestrator/allocator.py:reserve` | **FIXED in B-7a**: watchdog releases expired reservations (`status='reserved' AND expires_at < now()` → inventory `available`, order `released`) |
| 8 | `commit` expires_at check идёт в Python; SQL UPDATE не валидирует expires_at | `orchestrator/allocator.py:commit` | Wave B-7 (watchdog) |
| 9 | MagicMock на приватных _sync_* методах в test_allocator.py — pragmatic unit testing | `tests/test_allocator.py` | Wave B-5 (real DB integration tests) |
| 10 | `FOR UPDATE` with aggregate (MAX) in allocate_port_range_via_table → psycopg FeatureNotSupported | `orchestrator/jobs.py` | **FIXED in Wave B-5b** via pg_advisory_xact_lock |
| 11 | `process_refill_job` 3 except-ветки маркировали jobs failed без taxonomy в job_events; `refill.run_once` per-SKU exception откатывал весь cycle без visibility | `orchestrator/worker.py:process_refill_job` / `orchestrator/refill.py:run_once` | **FIXED in B-7b.4**: `log_job_event` вызывается в 3 except-ветках process_refill_job (request_error / runtime_error / unknown) с `error_type+error_class+attempts` через enriched event_data; refill.run_once per-binding try/except → log_job_event для post-insert exception, structured logger.warning для pre-insert. NO raise — worker loop continues. |
| 13 | screen-based schedulers умирают при reboot orchestrator-сервера (нет автостарта, потеря refill/validation/watchdog) | `scripts/start_schedulers.sh` | **FIXED in B-7a**: 3 systemd units (`netrun-orchestrator-refill/validation/watchdog`) с `Restart=always`; screen остаётся как fallback для dev-боксов |
| 14 | API paths inconsistent — `/nodes`, `/jobs` без префикса; `/v1/orders/*` с префиксом | `orchestrator/main.py` | **FIXED in B-7a**: `/v1/*` aliases добавлены для health/nodes/jobs; legacy paths остаются для backward compat, будут удалены в следующем major |
| 17 | Orchestrator port 8090 publicly accessible (`ORCHESTRATOR_HOST=0.0.0.0`, no firewall). Bot scanners attempt RCE via Log4Shell-style URLs (404'd, but visible in logs) | `install_orchestrator.sh` / `.env` | Wave B-7b (production hardening): bind to 127.0.0.1 + nginx HTTPS reverse proxy, OR ufw allow 8090 only from known IPs |
| 18 | enroll_node `ON CONFLICT (id)` падал с UniqueViolation на `nodes_url_key` когда нода уже была зарегистрирована через `add_node.sh` (random UUID) | `orchestrator/main.py:enroll_node` | **FIXED in Wave B-6.3**: switched to `ON CONFLICT (url)`, preserves existing id |

