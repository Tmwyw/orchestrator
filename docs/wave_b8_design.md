# Wave B-8 — Pay-per-GB Billing (design pass)

**Status:** design locked, awaiting execution split into B-8.1..B-8.4 sub-waves.
**Companion to:** `docs/wave_b_design.md` (which has the preliminary § 4.3 schema, § 6.8 endpoints list, § 8 architecture sketch — all expanded here).

---

## 1. Goal & non-goals

### 1.1 Goal

Add a second product line on top of the existing per-piece IPv6 inventory: **datacenter pay-per-GB** proxies billed by traffic volume, sold in discrete tier-bundles (1 / 3 / 5 / 10 / 20 / 30 GB), with a 30-day lease per purchase.

Each pergb buyer gets a **dedicated port** on a node (Variant α, locked in `wave_b_design.md § 3.1`). nftables per-port counters (already created by `proxyyy_automated.sh`) provide the byte-accounting source.

### 1.2 In scope (this wave)

- Postgres schema: `traffic_accounts`, `traffic_samples`; `proxy_inventory.status` ENUM extension to add `'allocated_pergb'`.
- Pydantic v2 response/request models for the new endpoints.
- Orchestrator endpoints:
  - `POST /v1/orders/reserve_pergb`
  - `POST /v1/orders/{order_ref}/topup_pergb`
  - `GET /v1/orders/{order_ref}/traffic`
  - `POST /v1/admin/traffic/poll`
- Node-agent endpoints (cross-repo `Tmwyw/node_runtime`):
  - `GET /accounting?ports=…`
  - `POST /accounts/{port}/disable`
  - `POST /accounts/{port}/enable`
- New systemd unit on orchestrator host: `netrun-orchestrator-traffic-poll.service` (the 6th, alongside the five from B-7a).
- New Prometheus metrics: poll counters, duration histogram, over-usage counter, counter-reset detection counter, lag gauge, account-state gauges.

### 1.3 Out of scope (deferred to Wave D / future)

- Hard per-port bandwidth cap at the nftables layer (the "(b) variant" of D5.5).
- Post-hoc clawback / over-usage billing recovery.
- Refund flow with `bytes_purchased` / `bytes_quota` two-column lifetime tracking (D6.6).
- Rotation-pack SKU pattern (D3.1) — purely orders.metadata.pack_id grouping, no schema changes needed when introduced.
- Per-user cap on number of active pergb accounts (anti-abuse — D3.3 future-concern).
- "Pause-and-recover" account state during node outages (D3.4 alternative recovery policy).
- Cumulative-tier loyalty pricing across purchases (D6.0 BrightData-style).
- TLS / external metric-scrape ACL (already covered by B-7b.5 nginx ACL).
- Webhooks/event-bus from orchestrator to bot (D7.1 Option a — bot stays poll-based).

### 1.4 Non-goals (we will *not* do these)

- Sharing a single port between users (Variant β rejected at `wave_b_design.md § 3.1`).
- Hot-resetting nftables counters during operation (would invalidate sample-diff math; counters monotonically accumulate, polling worker handles resets defensively).
- Cross-SKU top-up (D4.5: top-up requires same `sku_id` as parent purchase).

---

## 2. Schema

All changes against existing `migrations/` set. Three migrations.

### 2.1 `migrations/020_proxy_inventory_pergb_status.sql`

Extends the existing `proxy_inventory.status` CHECK enum with one new value `'allocated_pergb'`. Per D2.1, we use a distinct discriminator rather than reusing `'sold'` to keep existing per-piece queries (admin `/v1/admin/orders`, reporting SQL) unchanged — pergb rows are invisible to per-piece filters by design.

```sql
-- migrations/020_proxy_inventory_pergb_status.sql
ALTER TABLE proxy_inventory
  DROP CONSTRAINT IF EXISTS proxy_inventory_status_check;

ALTER TABLE proxy_inventory
  ADD CONSTRAINT proxy_inventory_status_check
  CHECK (status IN (
    'pending_validation',
    'available',
    'reserved',
    'sold',
    'expired_grace',
    'archived',
    'invalid',
    'allocated_pergb'
  ));
```

Note: `proxy_inventory.expires_at` for an `'allocated_pergb'` row is **NULL** (D2.2). Single source of truth is `traffic_accounts.expires_at`; the watchdog uses an explicit JOIN onto `traffic_accounts` for pergb cleanup. This keeps the lease state in one place and avoids drift between two columns when the operator extends one without the other.

### 2.2 `migrations/021_traffic_accounts.sql`

```sql
-- migrations/021_traffic_accounts.sql
CREATE TABLE traffic_accounts (
  id              BIGSERIAL PRIMARY KEY,
  order_id        BIGINT NOT NULL UNIQUE REFERENCES orders(id) ON DELETE CASCADE,
  inventory_id    BIGINT NOT NULL UNIQUE REFERENCES proxy_inventory(id) ON DELETE CASCADE,
  bytes_quota     BIGINT NOT NULL,
  bytes_used      BIGINT NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'depleted', 'expired', 'archived')),
  last_polled_at  TIMESTAMPTZ,
  last_polled_bytes_in   BIGINT,         -- anchor for diff math; NULL on first poll
  last_polled_bytes_out  BIGINT,
  depleted_at     TIMESTAMPTZ,
  expires_at      TIMESTAMPTZ NOT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_traffic_active_polling ON traffic_accounts(last_polled_at)
  WHERE status = 'active';

CREATE INDEX idx_traffic_expiring ON traffic_accounts(expires_at)
  WHERE status = 'active';

CREATE INDEX idx_traffic_inventory ON traffic_accounts(inventory_id);
```

Differences from the preliminary § 4.3 sketch in `wave_b_design.md`:

- Added `last_polled_bytes_in` / `last_polled_bytes_out` (anchor for sample-diff math; required for counter-reset detection per D4.4).
- Added `idx_traffic_inventory` (joined heavily by watchdog and by `/traffic` endpoint).
- `order_id UNIQUE` because per D4.5 a parent order has at most one `traffic_accounts` row; top-ups create new `orders` rows (linked via `metadata.parent_order_ref`) but **don't** create new `traffic_accounts` rows.

### 2.3 `migrations/022_traffic_samples.sql`

```sql
-- migrations/022_traffic_samples.sql
CREATE TABLE traffic_samples (
  id              BIGSERIAL PRIMARY KEY,
  account_id      BIGINT NOT NULL REFERENCES traffic_accounts(id) ON DELETE CASCADE,
  bytes_in        BIGINT NOT NULL,           -- cumulative reading at sample time
  bytes_out       BIGINT NOT NULL,
  bytes_in_delta  BIGINT NOT NULL,           -- diff vs previous sample (clamped >= 0 on counter reset)
  bytes_out_delta BIGINT NOT NULL,
  counter_reset_detected BOOLEAN NOT NULL DEFAULT FALSE,
  collected_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_samples_account ON traffic_samples(account_id, collected_at DESC);
```

`*_delta` columns let admin queries summarize without re-scanning across resets. Retention: app-side cleanup after 30 days (run from watchdog scheduler — added in B-8.2).

### 2.4 No `skus` schema changes

`skus.metadata` JSONB already exists. Tier table is stored as:

```json
{
  "tiers": [
    {"gb": 1,  "price_per_gb": "1.20"},
    {"gb": 3,  "price_per_gb": "1.10"},
    {"gb": 5,  "price_per_gb": "1.00"},
    {"gb": 10, "price_per_gb": "0.95"},
    {"gb": 20, "price_per_gb": "0.85"},
    {"gb": 30, "price_per_gb": "0.80"}
  ]
}
```

Per D1, `price_per_gb` is stored as **JSON string** to match the Decimal-as-string convention (`wave_b_design.md § 6.10`). Pydantic v2 `SkuTier` model in `api_schemas.py` validates the structure on load and on admin SKU edits. Format is intentionally extensible — future fields like `is_active`, `label`, `multi_currency_prices` can be added without migrations.

### 2.5 Snapshot at purchase / top-up

Per D1 + D6.2, `orders.metadata` records the tier snapshot for every financial transaction:

**Initial reserve_pergb** order row:
```json
{
  "chosen_tier_gb": 10,
  "tier_price_per_gb": "0.95"
}
```

**Top-up** order row:
```json
{
  "parent_order_ref": "ord_aaa123",
  "topup_sequence": 2,
  "chosen_tier_gb": 5,
  "tier_price_per_gb": "1.00"
}
```

Discriminator (per D6.2): if `parent_order_ref` ∈ metadata → top-up, else initial purchase.

`orders` rows are immutable for billing post-creation. Each transaction (reserve + every top-up) is its own row with its own `price_amount` = `gb_amount × tier_price_per_gb`. This gives a clean audit trail and historical analytics that survives tier-table edits in `skus.metadata`.

---

## 3. Node-agent contract (cross-repo `Tmwyw/node_runtime`)

This section is the spec for the `Tmwyw/node_runtime` patch that lands as part of B-8.1. All endpoints are HTTP(S), behind the same `X-API-KEY` gate (or open if `NODE_AGENT_API_KEY` is unset, mirroring `/health`/`/describe` policy from B-6.1).

### 3.1 `GET /accounting?ports=PORT[,PORT,...]`

Fetch cumulative byte counters for the listed ports.

**Implementation:** parse `nft -j list counters table inet proxy_accounting`, return per-port aggregates.

**Response (200):**
```json
{
  "32001": {"bytes_in": 102400, "bytes_out": 5242880, "bytes_in6": 1024, "bytes_out6": 524288},
  "32002": {"bytes_in": 0,      "bytes_out": 0,        "bytes_in6": 0,    "bytes_out6": 0}
}
```

Bytes are cumulative since counter creation (or since last reset — see § 8 edge cases).

The orchestrator's polling worker treats `bytes_in_total = bytes_in + bytes_in6` (and similarly for out) as the source value for diff math. IPv4 vs IPv6 split is for diagnostics only.

**Errors:**
- `400 invalid_ports` — port format malformed or out of range.
- `404 ports_not_found` — none of the requested ports have counters in the nft ruleset (likely the port was never bound or counters were destroyed). Per D5.1, polling worker handles partial responses defensively but **a fully-empty 404 from a node is still treated as a node failure**.
- `500` — nft command failed; polling worker treats as full-cycle failure.

**Defensive partial response:** per D5.1, if a node implementation accidentally returns 200 with only some of the requested ports populated, polling worker logs `traffic_poll_partial_response` warning and processes the known ports; missing ports are skipped to next cycle (no degraded-mark).

### 3.2 `POST /accounts/{port}/disable`

Stop traffic on the port without releasing the port reservation or destroying counters.

**Implementation requirements:**
- Stop the 3proxy instance for `{port}` *and/or* add a DROP rule blocking the port.
- **MUST NOT** delete the per-port nftables counter rule. The counter must continue to exist (frozen at current values) so subsequent enable→re-poll math anchors correctly.
- Idempotent (D7.7): calling `disable` on an already-disabled port returns `200 OK`, no-op.

**Response (200):** `{"status": "disabled"}`

**Errors:**
- `404 port_not_found` — port has no record in node-agent state (was never bound, or was reclaimed).

### 3.3 `POST /accounts/{port}/enable`

Resume traffic on a previously-disabled port.

**Implementation requirements:**
- Restart 3proxy instance for `{port}` and/or remove the DROP rule.
- **MUST NOT** reset the nftables counter — current cumulative values are the polling worker's anchor for the next cycle. Resetting here would create a phantom counter-reset event.
- Idempotent (D7.7): `enable` on an already-enabled port returns `200 OK`.

**Response (200):** `{"status": "enabled"}`

**Errors:**
- `404 port_not_found` — same semantics as disable.

### 3.4 nftables persistence (pre-condition, NORMATIVE)

Each node host MUST have nftables persistence configured (`/etc/nftables.conf` or `systemd-nftables` restore-on-boot) such that per-port counters **survive a node reboot**. This is a node-runtime / install_node.sh responsibility, **not** an orchestrator concern.

If a node violates this pre-condition, the polling worker still recovers safely via counter-reset detection (§ 8), but each reboot loses ~one polling-cycle of byte data for active accounts on that node.

### 3.5 Idempotency normative summary (D7.7)

| Endpoint | Already-in-state response | Unknown port |
|---|---|---|
| `POST /accounts/{port}/disable` | `200 OK` (no-op) | `404` |
| `POST /accounts/{port}/enable` | `200 OK` (no-op) | `404` |

Polling worker depends on this idempotency for safe retry on network blips.

---

## 4. Polling worker

New module: `orchestrator/traffic_poll.py` exporting `TrafficPollService.run_once() -> dict[str, int]`. Entrypoint: `orchestrator/traffic_poll_scheduler.py`. Systemd unit: `netrun-orchestrator-traffic-poll.service` (the 6th, alongside the five from B-7a).

### 4.1 Cadence

`TRAFFIC_POLL_INTERVAL_SEC` env var, default `60`. Clamped to `>= 30` in scheduler.

### 4.2 Single-cycle algorithm (`run_once`)

```
counters = {
  "accounts_polled": 0,
  "accounts_depleted": 0,
  "accounts_disabled": 0,
  "node_failures": 0,
  "counter_resets_detected": 0,
}

with serialization gate (D5.2):
  if previous run_once still executing → return early (skip this tick)

  Fetch active accounts grouped by node:
    SELECT t.id, t.inventory_id, t.bytes_quota, t.bytes_used,
           t.last_polled_bytes_in, t.last_polled_bytes_out,
           i.node_id, i.port
    FROM traffic_accounts t
    JOIN proxy_inventory i ON i.id = t.inventory_id
    WHERE t.status = 'active'
    GROUP BY i.node_id

  For each node (asyncio.gather, semaphore=TRAFFIC_POLL_CONCURRENCY default 16):
    try:
      response = node_client.get_accounting(node_url, ports=[...])
    except (timeout, 5xx):
      counters["node_failures"] += 1
      Increment node consecutive-failure counter.
      If consecutive_failures >= TRAFFIC_POLL_DEGRADE_AFTER (default 5):
        UPDATE nodes SET runtime_status='degraded' WHERE id = node_id
      Log `traffic_poll_node_failed`, skip cycle.
      Continue to next node.

    Reset node consecutive-failure counter to 0.

    For each port we got data for:
      account = lookup by inventory_id
      sample = response[port]
      bytes_in_total  = sample["bytes_in"]  + sample["bytes_in6"]
      bytes_out_total = sample["bytes_out"] + sample["bytes_out6"]

      If account.last_polled_bytes_in is None:
        # First poll — establish anchor, no delta yet
        delta_in  = 0
        delta_out = 0
        reset_detected = False
      Else:
        delta_in  = bytes_in_total  - account.last_polled_bytes_in
        delta_out = bytes_out_total - account.last_polled_bytes_out
        If delta_in < 0 OR delta_out < 0:
          # Counter reset (D4.4)
          counters["counter_resets_detected"] += 1
          Log `traffic_counter_reset_detected` warning event.
          delta_in  = 0          # don't bill reset gap
          delta_out = 0
          reset_detected = True
        Else:
          reset_detected = False

      INSERT INTO traffic_samples (account_id, bytes_in, bytes_out,
        bytes_in_delta, bytes_out_delta, counter_reset_detected)
        VALUES (account.id, bytes_in_total, bytes_out_total,
                delta_in, delta_out, reset_detected)

      new_bytes_used = account.bytes_used + delta_in + delta_out

      UPDATE traffic_accounts SET
        bytes_used = new_bytes_used,
        last_polled_bytes_in  = bytes_in_total,
        last_polled_bytes_out = bytes_out_total,
        last_polled_at = now(),
        updated_at = now()
      WHERE id = account.id

      If new_bytes_used >= account.bytes_quota AND account.status = 'active':
        UPDATE traffic_accounts SET status='depleted', depleted_at=now()
          WHERE id = account.id
        node_client.post_disable(node_url, port=account.port)
        counters["accounts_depleted"] += 1
        counters["accounts_disabled"] += 1
        Log `traffic_account_depleted` event.

      counters["accounts_polled"] += 1

return counters
```

### 4.3 Serialization gate (D5.2)

Implementation: in-process `asyncio.Lock` held for the duration of `run_once()`. Scheduler checks `lock.locked()` on each tick — if held, skips this tick (logs `traffic_poll_skipped_overlap` warning) and waits for next interval. Prevents overlapping cycles double-counting deltas.

### 4.4 Counter-reset detection contract

Polling worker MUST detect counter reset via `delta < 0` and skip the would-be-negative bill for that cycle (§ 8 edge case). This protects against:
- Node host reboot without nftables persistence.
- Operator manual `nft reset counter`.
- nft service restart.

The polling worker re-anchors on the new (lower) reading and resumes normal billing on the next cycle. One cycle's worth of byte data is lost (acceptable — see § 8).

### 4.5 Cleanup — folded into WatchdogService (NOT in poll worker)

Pergb-account maintenance lives in the existing `WatchdogService.run_once()` as a fifth phase, alongside the four per-piece cleanup phases from B-7a. Reasoning: WatchdogService already runs a single periodic cleanup pass; adding another worker just for pergb housekeeping doubles the systemd unit count for marginal benefit.

The pergb cleanup phase performs:

- Mark expired accounts: `UPDATE traffic_accounts SET status='expired' WHERE status IN ('active','depleted') AND now() >= expires_at` (mirrors per-piece `sold` → `expired_grace` lifecycle).
- Cascading inventory state: for the same `inventory_id`s, set `proxy_inventory.status` from `'allocated_pergb'` → `'expired_grace'` (preserving the unified inventory lifecycle from § 7.3).
- Archive after 3-day grace: `UPDATE traffic_accounts SET status='archived' WHERE status='expired' AND expires_at < now() - interval '3 days'`; cascading `proxy_inventory.status` → `'archived'`.
- Prune `traffic_samples` older than 30 days: `DELETE FROM traffic_samples WHERE collected_at < now() - interval '30 days'`.

`TrafficPollService` itself stays narrowly focused: read counters, write samples, update `bytes_used`, fire depletion-disable. No cleanup logic. This keeps the polling hot-path predictable in latency.

---

## 5. Tier pricing logic

### 5.1 Storage

`skus.metadata` JSONB, format documented in § 2.4. Pydantic v2 `SkuTier` and `SkuTierTable` models validate the shape on load + on admin SKU edits.

### 5.2 Tier selection (D6 — per-top-up bundle tier)

Each purchase or top-up specifies a `gb_amount`. The orchestrator looks it up in the SKU's tier table:

```python
def find_tier(sku: dict, gb_amount: int) -> dict:
    for tier in sku["metadata"]["tiers"]:
        if tier["gb"] == gb_amount:
            return tier
    raise InvalidTierError(gb_amount, available=[t["gb"] for t in sku["metadata"]["tiers"]])
```

Strict equality (D6.1). On mismatch → `400 invalid_tier_amount` with `available_tiers: [int]` in response body.

### 5.3 Pricing math

```
price_amount = gb_amount × tier.price_per_gb         # Decimal arithmetic
bytes_quota_added = gb_amount × 1024 × 1024 × 1024   # GB → bytes
```

`price_amount` is stored on the new `orders` row. `bytes_quota_added` is added to `traffic_accounts.bytes_quota`.

### 5.4 Tier table edits

Operators edit `skus.metadata.tiers` via admin SKU edit endpoint. Snapshot in `orders.metadata` ensures historical orders retain their original tier price (audit/refund safety).

If a tier is removed from a SKU after purchases at that tier exist, those purchases keep the snapshotted price; new purchases at the removed tier return `400 invalid_tier_amount`.

---

## 6. Order flow

### 6.1 `POST /v1/orders/reserve_pergb`

```
Request:
{
  "user_id": int,
  "sku_id": int,                     # MUST have product_kind='datacenter_pergb'
  "gb_amount": int,                  # MUST match a tier
  "duration_days": int | null,       # default = sku.duration_days
  "idempotency_key": str | null      # max_length=128
}

Sequence:
1. Pydantic validation.
2. Look up SKU; verify product_kind='datacenter_pergb'.
3. Validate gb_amount against SKU tiers; 400 invalid_tier_amount if no match.
4. Idempotency check: GET idem:reserve_pergb:{key} from Redis → return cached if hit.
5. Find available proxy_inventory row (status='available') for the SKU; equal-share
   across active sku_node_bindings (mirrors per-piece allocator).
6. Atomic claim: UPDATE proxy_inventory SET status='allocated_pergb' WHERE id=? AND status='available'.
7. INSERT INTO orders (
     order_ref='ord_'||hex, user_id, sku_id, status='reserved',
     requested_count=1, allocated_count=1,
     reservation_key=..., expires_at=now()+TTL,
     price_amount=gb_amount*tier_price, idempotency_key,
     metadata={chosen_tier_gb, tier_price_per_gb}
   )
8. INSERT INTO traffic_accounts (
     order_id=order.id, inventory_id=inventory.id,
     bytes_quota=gb_amount*1GB,
     status='active',
     expires_at=now()+duration_days*1d
   )
9. Set Redis reservation:{order_ref} TTL key (mirrors per-piece reserve).
10. Set Redis idem:reserve_pergb:{key} cached response (TTL=24h).
11. Return 200 with order_ref, traffic_url=/v1/orders/{order_ref}/traffic.

Response:
{
  "success": true,
  "order_ref": "ord_aaa123",
  "expires_at": "2026-05-29T...",
  "bytes_quota": 10737418240,
  "tier_price_per_gb": "0.95",
  "price_amount": "9.50",
  "traffic_url": "/v1/orders/ord_aaa123/traffic"
}

Errors:
400 invalid_tier_amount         {"available_tiers": [1,3,5,10,20,30]}
400 sku_not_pergb               product_kind != 'datacenter_pergb'
404 sku_not_found
409 insufficient_inventory      no available proxy_inventory row
```

### 6.2 `POST /v1/orders/{parent_order_ref}/commit`

Same as the existing per-piece `commit` endpoint — debit user, flip order status from `reserved` → `committed`, traffic_account already exists, no further action needed. The polling worker picks up the active account on its next cycle.

### 6.3 `POST /v1/orders/{parent_order_ref}/topup_pergb`

```
Request:
{
  "sku_id": int,                     # MUST equal parent order's sku_id (D4.5)
  "gb_amount": int,                  # MUST match a tier (D6.1)
  "idempotency_key": str | null
}

Sequence:
1. Pydantic + tier validation (same as reserve_pergb).
2. Verify sku_id matches parent's sku_id; 400 sku_mismatch_for_topup if not.
3. Lookup parent order + traffic_account; 404 if not found.
4. Verify account.status IN ('active', 'depleted'); 409 account_not_renewable if expired/archived (D4.1).
5. Idempotency check via Redis idem:topup_pergb:{key}; on hit, return cached.
6. Compute new totals atomically:
   - new_quota   = traffic_account.bytes_quota + gb_amount * 1GB
   - new_expires = MAX(traffic_account.expires_at, now() + parent_sku.duration_days)  (D4.2 α)
7. INSERT INTO orders (
     order_ref='ord_topup_'||hex, user_id, sku_id, status='committed',
     requested_count=1, allocated_count=1,
     price_amount=gb_amount*tier_price, idempotency_key,
     metadata={parent_order_ref, topup_sequence, chosen_tier_gb, tier_price_per_gb}
   )
   On UNIQUE-violation of idempotency_key → fetch existing row, return its response (D6.4 idempotency Path B).
8. UPDATE traffic_accounts SET
     bytes_quota = new_quota,
     expires_at  = new_expires,
     status      = CASE WHEN bytes_used < new_quota THEN 'active' ELSE status END,
     updated_at  = now()
   WHERE id = account.id
9. If status flipped depleted → active:
     node_client.post_enable(node_url, port=account.port)
     Log `traffic_account_reactivated` event.
10. Set Redis idem cache.
11. Return 200 with new totals.

Response:
{
  "success": true,
  "order_ref": "ord_topup_xyz",      # NEW top-up's own order_ref
  "parent_order_ref": "ord_aaa123",
  "topup_sequence": 2,
  "bytes_quota_total": 21474836480,
  "bytes_used": 5368709120,
  "expires_at": "2026-05-29T...",
  "tier_price_per_gb": "0.95",
  "price_amount": "9.50"
}

Errors:
400 invalid_tier_amount
400 sku_mismatch_for_topup
404 order_not_found
409 account_not_renewable          {"current_status": "expired"}
```

**URL/response order_ref naming clarification (D6.5):** `{order_ref}` in the URL path is the **parent** order's ref (the original `reserve_pergb`). The `order_ref` in the response body is the **new top-up's** order_ref. Bot devs MUST treat these as distinct.

### 6.4 `GET /v1/orders/{parent_order_ref}/traffic`

```
Response (200):
{
  "order_ref": "ord_aaa123",          # parent order_ref
  "status": "active",                 # active | depleted | expired | archived
  "bytes_quota": 10737418240,
  "bytes_used": 8053063680,
  "bytes_remaining": 2684354560,      # max(0, quota - used)
  "usage_pct": 0.75,                  # capped at 1.0 even if used > quota (D7.3)
  "over_usage_bytes": 0,              # bytes_used - bytes_quota if positive, else 0
  "last_polled_at": "2026-04-29T14:30:00Z",
  "expires_at": "2026-05-29T00:00:00Z",
  "depleted_at": null,
  "node_id": "node-x",
  "port": 32001
}

Errors:
404 order_not_found
404 traffic_account_not_found       detail: "this is a top-up order; use parent order_ref"
```

If called with a top-up's `order_ref` (which has no `traffic_accounts` row of its own), return 404 with the helpful detail. Defensive UX for bot devs.

### 6.5 `POST /v1/admin/traffic/poll`

Synchronous force-poll. Wired behind `require_api_key` like all admin endpoints.

```
Request (query params, all optional):
?node_id=NODE_ID    scope to one node
?account_id=ACCT_ID scope to one account

Response: (same shape as TrafficPollService.run_once() counters)
{
  "accounts_polled": 23,
  "accounts_depleted": 1,
  "accounts_disabled": 1,
  "node_failures": 0,
  "counter_resets_detected": 0
}
```

### 6.6 Sequence diagram — `reserve_pergb`

```
bot              orchestrator                           postgres        redis    node
 │                   │                                      │             │       │
 │POST reserve_pergb │                                      │             │       │
 ├──────────────────►│                                      │             │       │
 │                   │ validate tier                        │             │       │
 │                   │ idem cache check                     │             │       │
 │                   ├─────────────────────────────────────────────────►│       │
 │                   │ select+claim available inventory row │             │       │
 │                   ├─────────────────────────────────────►│             │       │
 │                   │ INSERT orders + traffic_accounts     │             │       │
 │                   ├─────────────────────────────────────►│             │       │
 │                   │ SET reservation:{ref} TTL            │             │       │
 │                   ├─────────────────────────────────────────────────►│       │
 │                   │ SET idem:reserve_pergb:{key}         │             │       │
 │                   ├─────────────────────────────────────────────────►│       │
 │ 200 + order_ref   │                                      │             │       │
 │◄──────────────────┤                                      │             │       │
 │POST commit        │                                      │             │       │
 ├──────────────────►│ debit balance, flip status=committed │             │       │
 │ 200               │                                      │             │       │
 │◄──────────────────┤                                      │             │       │
 │                   │                                      │             │       │
 │                   │ ─── polling loop @60s ───────────────────────────►│       │
 │                   │ GET /accounting?ports=...            │             │GET    │
 │                   │                                      │             │       │
 │                   │ ◄── byte counters ───────────────────────────────┤       │
 │                   │ INSERT traffic_samples + UPDATE bytes_used        │       │
 │                   ├─────────────────────────────────────►│             │       │
```

---

## 7. Lifecycle

### 7.1 traffic_account state transitions (D4.1)

```
                      bytes_used >= bytes_quota
       ┌─────► active ────────────────────────► depleted
       │         │                                 │
       │         │                                 │ top-up (D4.5)
       │         │                                 │ bytes_used < new_quota
       │         │                                 ▼
       │         │                              active (loops back)
       │         │
       │         │ now() >= expires_at
       │         ▼
       │      expired
       │         │
       │         │ + 3-day grace
       │         ▼
       │      archived (terminal — no transition out)
       │
   reserve_pergb (initial creation)
```

**Critical invariant:** there is **no transition from `expired` back to `active`**. Once expired, the account is dead. Top-up on expired accounts is rejected (`409 account_not_renewable`); user must `reserve_pergb` a fresh account, which may end up on a different port (the original port may have been reclaimed by another buyer in the grace window).

`depleted` ↔ `active` is the only round-trip transition. `bytes_used >= bytes_quota` flips `active → depleted` and disables the port; top-up flips back to `active` and re-enables the port (if `bytes_used < new_quota`).

### 7.2 Bot notification rules (D7 — bot-side state, documented for Wave C)

> **Placement note:** the bot's expected consumption behavior is documented here, alongside the `/traffic` endpoint that supplies the data, because the two ends of this interface need to be locked together. Bot consumption logic may migrate to a future `wave_c_bot_contract.md` when bot integration work begins; until then this section is the canonical spec for orchestrator API consumers.

The bot, **not the orchestrator**, owns notification dispatch. Documented here so Wave C bot devs have the spec.

**Quota thresholds:**
- Crossing thresholds: `0.75`, `0.90`, `1.0`.
- Bot stores `last_notified_threshold` per `traffic_account_id` in its own DB.
- On each hourly poll of `/v1/orders/{ref}/traffic`, compute `usage_pct = bytes_used / bytes_quota`.
- If `usage_pct >= next_threshold AND last_notified_threshold < next_threshold`, send notification, update `last_notified_threshold`.

**Highest-threshold-only on gap (D7.2):**
- If bot's hourly poll detects the account jumped past multiple un-notified thresholds in one interval (e.g., 60% → 100% in one cycle because of heavy traffic during gap), send **only** the highest threshold's notification. Skip lower ones — they are noise to the user.
- Set `last_notified_threshold` to the highest crossed threshold.

**Reset on top-up (D7.4):**
- When `bytes_quota` increases (detected via comparison to last-poll's value, or by detecting a new top-up `orders` row), recompute:
  - `last_notified_threshold = max(t for t in [0.75, 0.90, 1.0] if usage_pct >= t)`, defaulting to 0 if below all.
- Send recovery notification: "your account is reactivated, X GB available."

**Time-based expiry (D7.5):**
- Mirror per-piece pattern: `−3d`, `−2d`, `−1d` notifications before `expires_at`.
- Independent stream from quota thresholds; bot deduplicates per-stream.
- `expired` state triggers final "expired, buy new account" message.

### 7.3 Per-piece vs pergb lifecycle comparison

```
per-piece:    available → reserved → committed (sold) → expired_grace (3d) → archived
                  ▲                       │
                  │     extend            │
                  └───────────────────────┘

pergb:        active ⇄ depleted          (top-up loop)
                  │
                  └─── expired (time) ──→ archived (3d grace → terminal)
```

Per-piece `sold` is loosely analogous to pergb `active`; per-piece `expired_grace` to pergb `expired`. The **inventory** lifecycle is shared (`proxy_inventory.status` enum is unified), but the **business** lifecycle (in `traffic_accounts.status`) diverges.

---

## 8. Edge cases

### 8.1 Counter reset detection (D4.4)

**Trigger:** `delta_in < 0` or `delta_out < 0` in a single polling cycle.

**Handler:** clamp delta to 0 for that cycle, log `traffic_counter_reset_detected` event with `node_id`, `port`, `account_id`, `previous_anchor`, `new_reading`. Increment `netrun_traffic_counter_reset_total{node_id}` Prometheus counter. Re-anchor on new reading. Resume normal billing next cycle.

**Cost:** one cycle's worth of byte data lost on reset (acceptable — same loss as a single 5xx skip).

### 8.2 Polling outage and over-usage (D5.5)

**Trigger:** node returns 5xx for K consecutive cycles; user continues consuming bytes during the outage.

**Handler v1:** when polling resumes, the next successful sample's cumulative byte counter reflects all consumed bytes (including those consumed during the outage). Single big delta is applied; if `bytes_used` lands above `bytes_quota`, account flips to `depleted` immediately.

**Over-billing risk:** zero — counters are cumulative; recovery catches up exactly.

**Under-billing risk:** if the outage spans across `expires_at` (account marked `expired` before catch-up poll), the bytes consumed during the outage AFTER expiry are not billed. We accept this as part of D5.5.

**Hard cap (Wave D):** D5.5(b) future option — push per-port nftables rate-limit to node; trades off complexity for hard guarantee. Defer.

### 8.3 Partial response from `GET /accounting` (D5.1)

**Trigger:** node returns `200` with map missing some requested ports.

**Handler:** log `traffic_poll_partial_response` warning with `node_id` + `missing_ports`. Process available ports normally. Skip missing ports to next cycle. Do NOT mark node degraded (weak signal).

### 8.4 Polling cycle overlap (D5.2)

**Trigger:** `run_once()` takes > `TRAFFIC_POLL_INTERVAL_SEC` (e.g., 16 nodes × slow response).

**Handler:** in-process `asyncio.Lock` on the scheduler. If lock is held when next tick fires, log `traffic_poll_skipped_overlap` and skip this tick. Wait for next interval.

**Why critical:** without the gate, two cycles race on byte counter readings; second cycle reads counters that have already been deltaed by the first cycle → double-counts user bytes.

### 8.5 Node reboot without nftables persistence

**Trigger:** node host reboots; `/etc/nftables.conf` is missing or systemd-nftables not enabled; counters start from 0.

**Handler:** counter-reset detection (§ 8.1) catches it on next successful poll. One cycle of bytes lost; re-anchor proceeds.

**Pre-condition violation:** node-runtime install_node.sh MUST configure nftables persistence (§ 3.4). If repeatedly violated for a given node, ops sees high `netrun_traffic_counter_reset_total{node_id=...}` and investigates.

### 8.6 Race: poll cycle running while user traffic flowing

**Trigger:** between `SELECT bytes_used` and `UPDATE bytes_used`, user's traffic continues to register on nftables counters.

**Handler:** non-issue. The next poll cycle will read the new counter value and compute the delta naturally. There is no logical race because we always read a snapshot, compute delta vs anchor, and write the new anchor. User traffic during this window simply gets billed in the next cycle.

### 8.7 Top-up race: two concurrent top-up requests

**Trigger:** bot retries a top-up before idem cache is populated.

**Handler:** D6.4 idempotency Path B — INSERT on `orders` with `idempotency_key` triggers `UNIQUE` violation; orchestrator catches the duplicate-key error, fetches the existing row, returns its response. Bot does not double-debit. Same pattern as `reserve` idempotency (§ 7 below).

### 8.8 Disable race: poll cycle hits depletion exactly when user is mid-request

**Trigger:** polling worker detects `bytes_used >= bytes_quota`, calls `disable`, but a user TCP connection through the port is mid-flight.

**Handler:** node-agent disables the port (drops new connections + kills 3proxy instance). In-flight TCP connections may complete or drop depending on 3proxy behavior. A small amount of additional traffic (one in-flight transfer's tail) may land before the disable propagates — captured as `over_usage_bytes` in the next poll. Accept the small loss per D5.5.

### 8.9 Graceful node shutdown for maintenance

**Trigger:** operator stops the node-agent (`systemctl stop netrun-node-agent`) for maintenance.

**Handler:** orchestrator polling worker sees node failures → after K=5 cycles, marks node `runtime_status='degraded'`. New `reserve_pergb` allocations skip degraded nodes (refill engine respects `runtime_status='active'` filter). Existing accounts on the degraded node continue to consume bytes (3proxy + nftables remain running unless operator stops them too). On node-agent restart, polling worker recovers the byte data via the next successful sample.

**Recommendation for operators:** before maintenance, manually disable port via admin endpoint or pause refill — documented in operations.md update during B-8.4.

---

## 9. Migration plan — sub-waves

Split across 4 sub-waves to keep each diff reviewable and deployable independently.

### B-8.1 — Schema + node-agent contract

**Scope:**
- Migrations 020/021/022.
- Pydantic v2 models for new endpoints + `SkuTier`/`SkuTierTable` validators.
- `Tmwyw/node_runtime` patch: `GET /accounting`, `POST /accounts/{port}/disable`, `POST /accounts/{port}/enable` with idempotency; nftables persistence note in install_node.sh.
- Orchestrator stub endpoints (return `501 not_implemented` until B-8.2).

**Smoke:** mock node-agent returns dummy counters; `nft -j list counters` works on a real test node.

**Estimate:** 4-5 days. Cross-repo coordination required.

### B-8.2 — Polling worker + reserve_pergb + topup_pergb

**Scope:**
- `orchestrator/traffic_poll.py` + `traffic_poll_scheduler.py`.
- New systemd unit `netrun-orchestrator-traffic-poll.service`.
- `install_orchestrator.sh` installs the 6th unit.
- `orchestrator/traffic_poll.py` integration into `WatchdogService` for cleanup (or lightweight cleanup inside poll worker — pick during execution).
- `reserve_pergb` and `topup_pergb` endpoint implementations behind allocator with branching by `product_kind`.
- `/v1/orders/{ref}/traffic` endpoint.
- Prometheus metrics: `netrun_traffic_poll_total{node_id, status}`, `netrun_traffic_poll_duration_sec{node_id}`, `netrun_traffic_accounts_active`, `netrun_traffic_accounts_depleted`, `netrun_traffic_counter_reset_total{node_id}`, `netrun_traffic_poll_lag_sec`, `netrun_traffic_over_usage_total`, `netrun_traffic_bytes_total{sku_code, direction}`.
- Tests: 6+ unit tests for poll service (success, partial, full failure, counter-reset, depletion-trigger, top-up-reactivation).

**Smoke:** end-to-end against test node — purchase → traffic flows → polling captures bytes → depletion triggers disable → top-up reactivates.

**Estimate:** 1-2 weeks.

### B-8.3 — Admin endpoint + ops integration

**Scope:**
- `POST /v1/admin/traffic/poll` synchronous force-poll endpoint.
- Update `/v1/admin/stats` to include pergb subsection (active accounts count, total bytes consumed last 7d, top SKU by revenue).
- `docs/operations.md` updates: § 5 (6 systemd units), new § 12 "Pay-per-GB operations" with smoke test, force-poll usage, troubleshooting matrix for common edge cases (counter resets, partial responses, over-usage detection).

**Estimate:** 3-4 days.

### B-8.4 — Bot integration smoke + final docs

**Scope:**
- Cross-repo with Wave C bot work — by the time B-8.4 lands, bot has wired up reserve_pergb / topup_pergb / traffic endpoints.
- Real end-to-end purchase via bot UI: user buys 1 GB → traffic flows → 75/90/100% notifications fire → top-up → recovery notification.
- Real-money smoke (small amount, e.g. $1.20 for 1 GB).
- Close issue references in `wave_b_design.md`: § 8 status updated to "fully implemented in B-8.{1..4}".
- Update `docs/roadmap.md` § Phase 2 with B-8 closed.

**Estimate:** 3-4 days. Depends on Wave C bot scaffolding.

**Total estimate:** ~3 weeks across both repos.

### Coordination notes

- **B-8.1 cross-repo:** the `Tmwyw/node_runtime` PR for `/accounting` + `disable` + `enable` MUST land *before* B-8.2 deploys to prod (otherwise polling worker fails to find endpoints on existing nodes). Order of deploy: node_runtime PR merged + nodes upgraded → orchestrator B-8.2 deployed.
- **Existing prod nodes on `139.84.219.149` / `65.20.80.21` / `65.20.72.62`** need the node-agent upgrade before B-8.2. nftables ruleset already creates the per-port counters (`proxyyy_automated.sh` legacy behavior); only the new HTTP endpoints are missing.
- **Wave C bot work** can proceed in parallel with B-8.1/B-8.2 — bot integration is in B-8.4 and benefits from having a working orchestrator-side stack first.

### Deploy runbook (cross-repo sequencing for B-8.1 → B-8.2)

The orchestrator polling worker (B-8.2) depends on the node-agent endpoints (B-8.1) being live on every production node. Strict ordering required:

1. **Deploy node_runtime patch first** to all 3 production nodes (`139.84.219.149`, `65.20.80.21`, `65.20.72.62`). Restart `netrun-node-agent` on each. Smoke test:
   ```
   curl -s "http://NODE_IP:8085/accounting?ports="
   # → {} (empty object for empty ports list)
   curl -s "http://NODE_IP:8085/accounting?ports=32001"
   # → {"32001": {"bytes_in": ..., "bytes_out": ..., ...}} OR 404 if port not bound
   ```
2. **Wait 24h soak** — verify that per-piece flow on the upgraded nodes (refill jobs, `/generate` calls, validation) continues to work normally. Watch `journalctl -u netrun-node-agent -o cat | jq 'select(.level=="error")'` for any regressions introduced by the node-agent upgrade.
3. **Apply orchestrator migrations** 020/021/022 against production Postgres:
   ```
   cd /opt/netrun-orchestrator && python -m orchestrator.migrate
   ```
4. **Deploy orchestrator B-8.2**: `git pull` + `bash install_orchestrator.sh` (idempotent — will install the 6th systemd unit `netrun-orchestrator-traffic-poll.service` and restart all units).
5. **End-to-end smoke**: real pergb purchase via `scripts/test_purchase_pergb.sh` (added in B-8.2). Verify:
   - `reserve_pergb` succeeds and creates a `traffic_accounts` row.
   - Within 60s, the polling worker logs `traffic_account_polled` events with non-zero `bytes_in`/`bytes_out` once user actually uses the proxy.
   - `GET /v1/orders/{ref}/traffic` returns `usage_pct` rising as user generates traffic.
   - `top-up` flow works: small top-up (1 GB) extends `expires_at` and adds to `bytes_quota`.

This isn't runtime coupling (the bot → orchestrator → nodes call chain doesn't depend on B-8.2 being live; per-piece keeps working untouched), but **deploy ordering matters** because the polling worker on B-8.2 will call `/accounting` immediately on first cycle, and missing endpoints would generate noisy `traffic_poll_node_failed` warnings before the first successful sample.

---

## 10. Open questions / future decisions

These are deliberately deferred — not blockers for B-8 launch.

### 10.1 Cumulative-tier loyalty pricing (D6.0)

Industry pattern (BrightData, Soax): cumulative `gb_purchased` per account drives tier rate retroactively for new top-ups. Currently NETRUN locks per-top-up-bundle pricing (D6) which simplifies billing logic. If user feedback in Wave D shows "I want loyalty discounts like competitors," this is a candidate for a v2 pricing model. Implementation requires:
- New `traffic_accounts.gb_purchased_lifetime` column (or compute from `orders.metadata.chosen_tier_gb` aggregation).
- Top-up pricing math reads cumulative total + applies cumulative-tier rate.
- Open question whether parent-purchase price is retroactively credited (refund delta) or accepted as paid at locked rate. Cleaner: don't refund, just price future top-ups at the loyalty rate.

### 10.2 Refund flow — `bytes_purchased` vs `bytes_quota` two-column model (D4.3 / D6.6)

Refund of a top-up requires decrementing the user's allowance. With current single-column `bytes_quota`, refund either:
- Sets `bytes_quota -= refunded_bytes` (which can go below `bytes_used` — admin pain).
- Marks the top-up `orders` row as `status='refunded'` and recomputes `bytes_quota` from sum of non-refunded orders.

The cleanest model is `bytes_purchased` (immutable lifetime total bought) + `bytes_quota` (current allowance, can be admin-decremented). Add both columns in the Wave D refund-flow migration. Note: this also enables loyalty pricing (10.1) cleanly via `bytes_purchased`.

### 10.3 Per-user cap on active pergb accounts (D3.3)

Anti-abuse: one user reserving 50 accounts and saturating pool. Cap enforcement options:
- Hard limit: `MAX_ACTIVE_PERGB_PER_USER` env var, allocator rejects with `409 user_account_limit_reached`.
- Soft limit + admin alert: log a warning when user crosses threshold (e.g. >5 active accounts).

Defer to Wave D once we have user behavior data.

### 10.4 Recovery policy for active accounts on offline nodes (D3.4) — **preferred Wave D path**

Two alternatives, **pause-and-recover preferred**:

- **Pause-and-recover (preferred):** `status='paused'` during outage, billing frozen, `expires_at` extends by outage duration. Account resumes when node returns. Port stays the same.
- **Migrate-fresh (alternative):** on prolonged outage, terminate the account (`status='archived'`), refund unused quota proportionally. User buys new account on fresh node — port changes, scraper config changes.

Why pause-and-recover wins:
- **Lower deploy risk:** orchestrator-only change, no node-runtime contract changes required.
- **Honest to user:** most outages are short (minutes); freezing the lease + extending `expires_at` is the user-friendly default.
- **Single mental model with D3.4** already noted in the design.
- **Auto-retires D5.5 over-usage concern** as a side benefit: while paused, no traffic flows → no over-usage to track.

Wave D implementation requires:
- New `paused` state in the lifecycle diagram (sixth state alongside active/depleted/expired/archived).
- Polling worker detects degraded-node-with-paused-accounts on recovery and resumes.
- Outage-duration tracking on `traffic_accounts` (e.g. `paused_at TIMESTAMPTZ` + `outage_extension_sec BIGINT`).

Migrate-fresh stays as the documented alternative for the edge case where a node is permanently lost (hardware failure, provider outage > X days). Operator picks per-incident which path to take.

### 10.5 Hard per-port nftables bandwidth cap (D5.5(b))

Push `bytes_quota` to node-agent as a per-port nftables rule that drops traffic at the cap. Removes over-usage entirely. Cost: non-trivial nftables rule generation (need to atomically swap when quota changes), node-agent contract gains `POST /accounts/{port}/quota` endpoint with validation logic, edge cases around partial-state during quota updates.

**Status: reserved for the case where ops shows persistent over-usage even after § 10.4 (pause-and-recover) is deployed.** § 10.4 is the preferred path because it has lower deploy risk and is orchestrator-only; § 10.5 is the layer-on-top fallback if ops data demands stronger guarantees.

### 10.6 Rotation pack SKU (D3.1)

User-facing "5-IP rotation pack" without changing the 1:1 schema:
- Bot creates `N` separate `traffic_accounts` rows, each with own port + own quota share.
- Orders share `metadata.pack_id = uuid` so admin/bot views can JOIN.
- `pack_id` lives **only** on `orders.metadata` (D3.1) — no denormalization to `traffic_accounts.metadata`.

No B-8 implementation needed; just keep the API surface 1:1-account-shaped for now.

### 10.7 Shared `/v1/orders/expiring` endpoint vs separate per-product (D7.5)

For Wave C bot: poll one endpoint that returns both per-piece and pergb expiring soon, with `product_kind` discriminator? Or two separate endpoints?

**Proposed:** shared. Bot polls one endpoint, gets list of `(order_ref, product_kind, expires_at, ...)`, formats notification per product. Cleaner bot code. Defer the detailed shape to Wave C.

### 10.8 Prometheus metric cardinality

Per-account cardinality (`netrun_traffic_bytes_total{account_id=...}`) explodes Prometheus when the user count grows. We use `{sku_code, direction}` instead (low cardinality). When fleet hits 10k+ accounts, even per-node aggregates may need attention — defer to Wave D scaling pass.

### 10.9 GIN index on `orders.metadata`

Top-up admin queries (`WHERE metadata->>'parent_order_ref' = $1`) work without an index at our current scale. If reporting performance becomes an issue at 100k+ orders, add `CREATE INDEX idx_orders_metadata_gin ON orders USING gin (metadata jsonb_path_ops)`. Defer to Wave D.

### 10.10 Admin-side notification stream (operator alerts)

Counter-reset events, repeated node failures, over-usage events should flow to ops (Telegram channel? PagerDuty? Grafana alert rules?). Not in B-8 scope; falls under Wave D § D.6 monitoring + alerting.

---

## 11. Idempotency strategy summary (cross-cutting)

Per D6.4, the billing flow uses two layered idempotency mechanisms:

**Layer 1 — Redis idem cache:**
- Key: `idem:{endpoint}:{idempotency_key}`. TTL: 24 hours.
- Set on every successful response.
- Read at start of every request; if hit, return cached response without re-executing logic.

**Layer 2 — Postgres `orders.idempotency_key UNIQUE`:**
- INSERT with same `idempotency_key` raises `UNIQUE` constraint violation.
- Handler catches the violation, fetches the existing row, returns its response.
- Covers race condition where Layer 1 cache write failed but DB insert succeeded.

**Both layers cover end-to-end:**
- Network blip during request → bot retries with same `idempotency_key`.
- Path A (cache hit): Layer 1 returns cached response.
- Path B (cache miss but DB row exists): Layer 2 returns existing row's response.
- Path C (neither — first call truly aborted before DB write): retry executes normally.

This pattern applies to `reserve`, `reserve_pergb`, `topup_pergb`, `commit`. Documented here as the canonical billing-flow idempotency contract.
