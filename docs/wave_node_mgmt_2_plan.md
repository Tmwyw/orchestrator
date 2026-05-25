# Wave NODE-MGMT-2 — auto degraded→active recovery + DNS passthrough

Orchestrator-only follow-up to Phase 2 (NODE-MGMT). No migrations — uses
`runtime_status` / `heartbeat_failures` columns already added by
`migrations/003_extend_nodes.sql`.

## Goals

* **A.** When a node returns to reachable state, automatically flip its
  `runtime_status` back from `degraded` → `active` and reset
  `heartbeat_failures = 0`. Today this only happens via the manual
  `PATCH /v1/admin/nodes/{id}` endpoint in `admin_nodes.py`.
* **B.** Forward-compatible passthrough of a per-node `dns` field from
  the node-agent's `/health` payload through `GET /v1/nodes/health` into
  the bot's health panel. Raw, no schema validation — old nodes that
  don't send `dns` get `null` in the response item.

## Branch / baseline

* Branch: `wave/node-mgmt-2` from `origin/main` (= `1226f43`).
* Pytest baseline pre-wave: **337 passed / 0 skipped**.

## Stage A — auto recovery wiring

Inside `nodes_health()` (`orchestrator/main.py`), the existing fan-out
already builds the list of reachable node ids and bulk-updates
`last_heartbeat_at`. In the same DB session, add a sibling UPDATE that
flips degraded nodes back to active for reachable ids only:

```sql
update nodes
set runtime_status='active',
    heartbeat_failures=0,
    updated_at=now()
where id = ANY(%s)
  and runtime_status='degraded'
```

* Same `%s` list as `last_heartbeat_at` — reachable-only.
* `runtime_status='degraded'` predicate keeps the update idempotent and
  ensures we never touch `disabled` / `offline` (those are admin-set).
* Log every recovered id via `logger.info("node_auto_recovered", ...)`.
* Best-effort: wrapped in the same try/except as the heartbeat update,
  failure logged but never blocks the response.

**Cadence note.** Auto-recovery fires only when `/v1/nodes/health` is
requested. The bot's «🩺 Здоровье» panel hits it on demand (with a 30 s
cache). That is enough for the operator-driven flow we have today. A
strictly guaranteed cadence would need either a scheduled call or a
sweep inside `watchdog.run_once()` — out of scope for this wave.

## Stage B — DNS passthrough

* `_ping_one_node()` returns the `dns` value extracted from the node's
  `/health` payload (`result.get("dns")`). Raw, no validation.
* The item-dict built in `nodes_health()` gains `"dns": <raw or None>`.
* Unreachable nodes (no payload) get `dns: null` by construction.

The existing contract (`reachable` / `latency_ms` / `last_check` /
`runtime_status` / `id` / `name` / `geo`) is unchanged — `dns` is
purely additive.

## Stage C — tests

Add to `tests/test_nodes_health_endpoint.py`:

* `degraded → active` auto-recovery happens for reachable nodes
  (captured via the existing `_patch_connect` cursor recorder).
* Reachable + already `active` — no recovery UPDATE issued (still only
  the heartbeat one).
* Unreachable degraded node stays `degraded` (no recovery for it).
* `disabled` / `offline` nodes that happen to be reachable do NOT
  flip to `active`.
* DNS passthrough: payload with `dns: {...}` flows through unchanged;
  payload without `dns` → item has `dns: null`; unreachable → `null`.

Existing tests must continue to pass — none of the established
contract fields change shape.

## Journal

* (filled in as work progresses)
