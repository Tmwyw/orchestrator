# Wave PERGB-METER-FIX — Pay-per-GB metering rebuild (client-port metering + nft quota hard cap)

Status: DESIGN (2026-06-13). Cross-repo: `node_runtime` (generator + node-agent) + `netrun-orchestrator`.
Author: design pass over live + code root-cause.

---

## 1. As-is + root cause

Pay-per-GB billing captures ~0% of real download volume. Each port gets three nft counters in
`proxyyy_automated.sh` (per-port loop ~L1095–1145): `proxy_<port>_in` = `input tcp dport <port>`
(client→proxy IPv4, only requests/ACKs, tiny), `proxy_<port>_out` = `output ip6 saddr <FIXED_IPv6>`,
and `proxy_<port>_in6` = `input ip6 daddr <FIXED_IPv6>` (the actual download). The two byte-heavy
rules match a **single fixed IPv6** that was generated once at provision time and pinned into the
3proxy listener with `-e<ipv6>` (L881). But the fleet runs `strict_dual_stack` (default, `server.js:42`;
mode flag `-64`), so 3proxy egresses over **IPv4** whenever the destination/network prefers it, and the
rotating-IPv6 design can also use an address other than the pinned one. In every such case the
download's source/dest IPv6 does **not** equal the rule's `saddr`/`daddr`, the packet matches no
counter, and the bytes vanish. Only the small IPv4 client-side `_in` rule fires — hence a multi-GB
download showed a 0.3 MB top counter in production. The metering anchor (egress address) is the one
thing about the connection we do **not** control; the client-facing port is the one thing we **do**.

---

## 2. New metering — meter on the client-facing port (family-agnostic)

### 2.1 Principle

The client always connects **to** `<port>` and the proxy always replies **from** `<port>`, for both
IPv4 and IPv6 clients, regardless of which family/address is used for the upstream egress. So we count
on the fixed listening port and ignore egress entirely. We replace the egress-IPv6 rules with four
client-port rules per port (`ip` = v4 + `ip6` = v6, in both directions).

`bytes_used` for pay-per-GB = bytes **delivered to the client** = the `output tcp sport <port>` total.
This is exactly the billable download volume the customer paid GBs for. We still count the inbound
(`input tcp dport`) leg for observability/symmetry, but the billable number is the egress-to-client leg.

### 2.2 New nft rule set per port (`proxyyy_automated.sh`, replace the L1095–1160 block)

Two named counters per port (down from three). One counter accumulates both address families by adding
two rules that point at the **same** counter object — nft sums them natively:

```
# --- per port $port ---
nft delete rule    inet proxy_accounting input  tcp dport "$port" 2>/dev/null || true   # legacy cleanup
nft delete rule    inet proxy_accounting output ip6 saddr "$old_ipv6" 2>/dev/null || true
nft delete rule    inet proxy_accounting input  ip6 daddr "$old_ipv6" 2>/dev/null || true
nft delete counter inet proxy_accounting "proxy_${port}_in"  2>/dev/null || true
nft delete counter inet proxy_accounting "proxy_${port}_out" 2>/dev/null || true
nft delete counter inet proxy_accounting "proxy_${port}_in6" 2>/dev/null || true   # retire the v6-only counter

nft add counter inet proxy_accounting "proxy_${port}_in"  2>/dev/null || true
nft add counter inet proxy_accounting "proxy_${port}_out" 2>/dev/null || true

# client -> proxy (upload), both families, same counter:
nft add rule inet proxy_accounting input  ip  daddr-port-irrelevant tcp dport "$port" counter name "proxy_${port}_in"  comment "proxy_${port}_in"
nft add rule inet proxy_accounting input  ip6 tcp dport "$port" counter name "proxy_${port}_in"  comment "proxy_${port}_in"

# proxy -> client (DOWNLOAD = billable), both families, same counter:
nft add rule inet proxy_accounting output ip  tcp sport "$port" counter name "proxy_${port}_out" comment "proxy_${port}_out"
nft add rule inet proxy_accounting output ip6 tcp sport "$port" counter name "proxy_${port}_out" comment "proxy_${port}_out"
```

> Implementation note: in a real `inet` table the family qualifier is `meta nfproto ipv4` / `meta nfproto ipv6`
> (or just drop the qualifier and match `tcp dport`/`tcp sport` directly — in an `inet` table a bare
> `tcp dport <port>` rule already matches **both** v4 and v6). The simplest correct form is **family-agnostic**:
>
> ```
> nft add rule inet proxy_accounting input  tcp dport "$port" counter name "proxy_${port}_in"  comment "proxy_${port}_in"
> nft add rule inet proxy_accounting output tcp sport "$port" counter name "proxy_${port}_out" comment "proxy_${port}_out"
> ```
>
> Two rules per port (not four). `inet` tables see both families on the same hooks, so one `tcp dport`
> rule counts v4+v6 clients and one `tcp sport` rule counts v4+v6 download. This is the recommended form;
> the explicit-family variant above is only needed if a future need to split v4/v6 billing arises.

Counter names stay `proxy_<port>_in` / `proxy_<port>_out`. We **drop `_in6`** entirely (it was the
broken v6-daddr rule). `_in` now means "client upload (both families)", `_out` now means "download
delivered to client (both families) = billable".

### 2.3 `accounting.js` changes (node-agent)

- `COUNTER_NAME_RE` (L11): change `/^proxy_(\d+)_(in6|in|out)$/` → `/^proxy_(\d+)_(in|out)$/`.
  Drop `in6`. (Keeping `in6` in the regex is harmless if a legacy node still has it, but a
  freshly-regenerated node won't emit it — see migration §4 for the mixed-fleet window.)
- `getCountersForPorts()` per-port aggregation (L147 / L154–160): the bucket no longer needs `in6`.
  Change the return shape (L156–159) to:
  ```js
  out[String(port)] = {
    bytes_in:  b.in,    // client -> proxy (upload), both families
    bytes_out: b.out,   // proxy -> client (download), both families = BILLABLE
  };
  ```
  Remove the `b.in + b.in6` sum. The function signature, the `/accounting` route (`server.js:2875`),
  and the `{success, counters}` envelope are unchanged.

### 2.4 `traffic_poll.py` changes (orchestrator)

- `_process_sample()` (L226–227): the `bytes_in6`/`bytes_out6` fields are now confirmed-dead
  (node-agent never emits them). The `.get(..., 0)` defaults already make removing them safe, but for
  clarity change to:
  ```python
  bytes_in_total  = int(sample.get("bytes_in", 0))
  bytes_out_total = int(sample.get("bytes_out", 0))
  ```
  Billing semantics are unchanged: `new_port_snapshot += delta_in + delta_out` (L253). After the fix
  `delta_out` carries the real download for the first time, so `bytes_used` finally tracks reality.
- **Decision point (billing policy):** today `bytes_used` = upload + download (`delta_in + delta_out`).
  Recommend keeping that (count both directions) — it's simpler and slightly conservative in the
  house's favour. If product wants "download only", change L253 to `+ delta_out` only. Flag for §7.
- `node_client.get_accounting()` docstring (L108) still claims a 4-field shape; update to the 2-field
  `{bytes_in, bytes_out}` shape. No logic change (it just merges whatever the node returns).

No DB-schema change for metering: `proxy_inventory.bytes_used_snapshot` / `last_polled_bytes_in/out`
and `traffic_accounts.bytes_used` are all reused as-is.

---

## 3. nft-quota hard cap (kernel byte-cap)

### 3.1 What the quota is

An nft `quota` object enforces a byte ceiling **in the kernel**: once the matched traffic crosses the
limit, the rule's verdict applies to the next packet. We attach it to the **download (`output tcp sport
<port>`)** path so the kernel itself cuts the user at the byte boundary, independent of the 30–60s poll
interval. This is the true zero-overshoot backstop the current SIGKILL-on-poll path can't provide
(poll lag + SIGTERM grace let an in-flight download run over).

Per-port nft object + rule (added alongside the counter rules in §2.2):

```
# quota seeded to this port's share of the user's budget (see §3.4 for the split)
nft add quota inet proxy_accounting "quota_${port}" over ${port_budget_bytes} bytes
nft add rule  inet proxy_accounting output tcp sport "$port" quota name "quota_${port}" drop
```

`over … drop`: while under the limit the rule does nothing (packet falls through to the counter rule
and is delivered); once over, matching packets are **dropped** in-kernel. The download stalls at the
boundary. We keep the counter rule separate so we still observe bytes for billing/aggregation even
after the kernel cap engages (counter rule ordered **before** the quota-drop rule so the bytes that
triggered the cap are still counted).

> Ordering caveat: nft evaluates rules top-to-bottom within a chain. Place the `counter` rule **above**
> the `quota … drop` rule for the same `tcp sport` so the last (over-limit) packet is counted before it's
> dropped — otherwise the kernel drops it and `bytes_used` undercounts by one packet (negligible, but
> keep the order deterministic).

### 3.2 How the quota reaches the node

New node-agent endpoint (mirrors the existing `/accounts/{port}/disable|enable` family in
`server.js:2886`):

```
POST /accounts/{port}/quota   body: { "bytes": <int> }   -> { success, port, bytes }
```

Implemented in `accounting.js` as `setQuota(port, bytes)`:
- `nft list quota inet proxy_accounting quota_<port>` to check existence;
- if missing: `nft add quota inet proxy_accounting quota_<port> over <bytes> bytes` + add the drop rule;
- if present: `nft reset`-then-`add` is racy, so use the in-place update —
  `nft add quota inet proxy_accounting quota_<port> { over <bytes> bytes used <current_used> bytes }`
  is **not** an in-place edit. nft has no atomic "change the ceiling, keep the used" verb, so the
  pragmatic approach is: read current `used` from the quota object, delete + re-add with
  `over <new_bytes> bytes used <preserved_used> bytes`. This preserves consumed bytes across a topup so
  a topup correctly *raises the ceiling* rather than resetting consumption. Wrap in an `nft -f` atomic
  batch (single transaction) so there's no window with no quota rule.
- 404 (`PortNotFoundError`) if no config for the port, matching the disable/enable contract.

### 3.3 When the orchestrator pushes the quota

The quota is a **derived, best-effort backstop**, not the source of truth (that stays the poll
aggregator — see §3.4). It is pushed at the same three moments `post_enable` already fans out, so we
piggyback on existing call sites in `pergb_service.py`:

| Event | Existing hook | New action |
|---|---|---|
| **reserve_pergb** reactivates a depleted/expired pool (`pergb_service.py:215`) | `_best_effort_post_enable_all` | after enable, `_best_effort_set_quota_all` with the new per-port share |
| **generate_ports** allocates new ports | port-allocation path | set quota on each freshly-allocated port at enable time |
| **topup_pergb** grows the budget (`pergb_service.py:432`) | `_best_effort_post_enable_all` | `_best_effort_set_quota_all` recomputes + raises every port's quota |
| **generate-time** (node) | `proxyyy_automated.sh` | seed `quota_<port>` to a large default (or 0=unlimited sentinel); orchestrator immediately corrects it on first enable |

A new `node_client.post_set_quota(url, api_key, port, bytes)` mirrors `post_enable`. New
`_best_effort_set_quota_all(account_id)` mirrors `_best_effort_post_enable_all`: fetch linked ports,
compute the per-port byte share, POST quota to each, log per-port, never fail the user-facing op on a
node hiccup (watchdog re-pushes — §3.4).

### 3.4 The hard part — per-USER pool vs per-PORT quota (honest treatment)

**The mismatch is real and unavoidable.** The product model is ONE GB pool per user spanning N ports
across M nodes, all usable simultaneously. An nft quota is a kernel object local to one port on one
node. There is **no** cross-node shared kernel counter. So a single per-user budget cannot be enforced
as a single hard cap. Two layers, with clearly separated jobs:

**Layer A — poll-based aggregation = the source of truth (unchanged, already correct after §2).**
`traffic_poll._aggregate_and_flip_depleted` already does the right thing: it SUMs
`bytes_used_snapshot` across **all** the user's ports on **all** nodes into `traffic_accounts.bytes_used`,
compares to `bytes_quota`, flips to `depleted`, and fans out `post_disable` (SIGTERM→SIGKILL) on every
port. This is the only layer that sees the whole pool. It stays the authority for "is the user out of
GB". After §2 it finally has real numbers.

**Layer B — per-port nft quota = a local safety backstop, deliberately loose.**
The per-port quota is **not** meant to enforce the exact pool total. Its job is to stop a *single
runaway port* from burning unbounded bytes inside one poll interval (the revenue-leak window). So set
each port's quota to a **generous fraction that can't itself overshoot the whole budget catastrophically**:

- **Recommended split:** `port_budget_bytes = bytes_quota` (the *full* pool budget) on **every** port.
  Rationale: any single port can at most deliver the whole budget before its kernel cap engages — which
  is exactly the worst case the poll layer would catch anyway, but now bounded in-kernel instead of
  unbounded. The poll layer still cuts the *pool* the instant the SUM crosses the budget; the per-port
  cap only matters if one port races ahead between polls. Setting it to the full budget means the cap
  never fires prematurely (no false cut while the user still has pool budget) but still bounds the
  damage from any one port to ≤ one budget's worth of overshoot — vastly better than unbounded.
- **Why not split `bytes_quota / N` per port?** Because the pool is "all usable simultaneously" — a user
  with 10 GB across 5 ports might legitimately push 9 GB through one port and 1 GB through the others.
  An even `/N` split would kernel-drop that port at 2 GB while 8 GB of pool budget sits unused → broken
  product. The poll aggregator handles fairness across ports; the per-port cap must be permissive enough
  never to fight it. **Use the full-budget-per-port cap.**

**Net effect:** poll layer = accurate cross-node depletion + SIGKILL cut (authority). nft quota =
per-port in-kernel ceiling that bounds the single-port overshoot during the poll-lag window to at most
one budget. Honest limitation: a *deliberately abusive* user spreading a burst across many ports could,
in the worst theoretical case, push up to `N × bytes_quota` extra before the next poll cuts everything —
but realistically a burst lands on one or few ports, the poll interval is 30–60 s, and the SIGKILL
severs live sessions. This is a pragmatic, shippable bound, not a perfect distributed cap. Building a
true cross-node real-time counter (shared state in Redis read by every node on every packet) is out of
scope and not worth it.

### 3.5 Interaction with depletion / reactivation / SIGKILL

- **Depletion (poll):** unchanged. `_aggregate_and_flip_depleted` flips status + `_fire_disable_account`
  fans out `post_disable` → SIGTERM→(grace)→SIGKILL (`accounting.disablePort`). The nft quota is
  orthogonal: a disabled port has no running 3proxy so the quota rule is moot until re-enabled.
- **Reactivation (topup/rebuy):** `topup_pergb` raises `bytes_quota`, flips `depleted→active`, fans out
  `post_enable` **and now** `_best_effort_set_quota_all` to raise every port's `quota_<port>` ceiling.
  Because we preserve the quota's `used` across the update (§3.2), a topup correctly resumes from where
  the user was, not from zero. The `_disableGen` generation guard in `accounting.js` already protects
  the enable from a stale in-flight SIGKILL; the quota push is a separate idempotent nft op with no race
  against it.
- **Order on topup:** raise DB `bytes_quota` → `post_enable` (spawns 3proxy) → `set_quota` (raises kernel
  ceiling). If `set_quota` is pushed before enable, the rule simply sits on a not-yet-running port
  (harmless); if after, the proxy briefly runs under the *old* ceiling — so push set_quota promptly, but
  it's best-effort and the next poll/watchdog reconciles. Recommend enable-then-quota.

---

## 4. Migration — apply to already-sold/live ports without disruption

The live fleet has the broken 3-rule (`_in`/`_out`/`_in6` IPv6-pinned) set on every sold port. We must
swap to the 2-rule client-port set **without restarting 3proxy** (restart = dropped customer sessions).

**Key fact that makes this safe:** changing nft rules does **not** touch 3proxy. The proxies keep
running; only the kernel accounting rules change. So migration is a node-local nft re-apply.

### 4.1 Node-side: a `reaccount` operation

Add a node-agent endpoint / generator entrypoint `reaccount_all_ports` that, for every existing
`3proxy_<port>.cfg` in `PROXY_CFG_DIR`:
1. deletes the 3 legacy rules + `_in6` counter for that port (the `nft delete … 2>/dev/null || true`
   cleanup already in §2.2);
2. adds the 2 new client-port counter rules;
3. (if quota enabled) adds `quota_<port>` seeded to a large default.

This is the **same code path** as fresh generation §2.2 minus spawning proxies — factor §2.2 into a
shared `setup_port_accounting(port, old_ipv6)` function and loop it over the existing config files.

**Counter reset on swap (important):** deleting + re-adding a counter resets it to 0. `traffic_poll`
already handles this: on the next poll the new `_out` reading is far **lower** than the stored
`last_polled_bytes_out` anchor → `delta < 0` → `traffic_counter_reset_detected` → delta clamped to 0,
anchor reset to the new reading (`traffic_poll.py:237–251`). No negative billing, no double-count. The
user simply starts accruing real bytes from the swap moment forward. **This is acceptable and expected**
— we cannot retroactively recover the bytes the broken rules never counted anyway.

### 4.2 Orchestrator-side ordering

1. **Deploy node-agent + generator** with the new `setup_port_accounting` + `reaccount` + `setQuota`
   endpoint to the fleet first (node-agent is backward-compatible: it still answers `/accounting` in the
   2-field shape, and the orchestrator already tolerates missing fields).
2. **Run `reaccount` across the fleet** (orchestrator admin command iterating nodes, or a one-shot
   script). Each node swaps its rules live. Poll cycles during the swap just see a reset (handled).
3. **Deploy orchestrator** with the `accounting.js`-consuming changes already merged (the 2-field read
   is a no-op against either old or new node since `.get(...,0)` defaults cover the dropped fields) and
   the new quota-push call sites. Orchestrator can ship before or after step 2 — it's tolerant both ways.
4. **Enable quota push** behind a config flag (`ORCHESTRATOR_PERGB_NFT_QUOTA_ENABLED`, default off →
   on) so layer B can be rolled out separately from the metering fix and rolled back instantly if a
   node's nft version misbehaves.

Old un-reaccounted nodes keep working (they just keep under-counting until reaccounted) — there's no
hard cutover, so the fleet can be migrated node-by-node.

---

## 5. Risks / edge cases

- **IPv6 clients:** handled by the family-agnostic `inet` `tcp dport/sport` match — one rule counts both
  v4 and v6 clients. This is the whole point of moving the anchor to the port.
- **Dual (http+socks) ports double-count:** a `dual` proxy emits a socks listener on `<port>` **and** an
  http listener on `<port-10000>` (`proxyyy_automated.sh:887`). These are **two different ports**, so
  they get **two different counters** — no double-count on a single port. BUT: the orchestrator must
  meter **both** ports of a dual pair against the same pool, and seed a quota on **both**. Check
  `proxy_inventory` carries both the socks and the paired http port as separate rows linked to the same
  `traffic_account` — if the http port (`port-10000`) is not a polled `proxy_inventory` row, its download
  is uncounted again. **Action item:** confirm dual http ports are rows in `proxy_inventory` (and thus in
  `_fetch_active_ports`); if not, add them or meter the pair under the socks port. Flag for §7.
- **Same-counter double-count:** pointing two rules (v4+v6) at one counter is additive and correct; do
  **not** also keep a bare combined rule, or bytes count twice. The recommended single family-agnostic
  rule per direction avoids this entirely.
- **Loopback / health-check traffic on the port:** node-agent health pings or local probes hitting
  `<port>` would count. In practice negligible; if a node self-tests proxies, scope the counter rules to
  exclude `iif "lo"` on the input side. Minor.
- **`_in6` left in `COUNTER_NAME_RE`:** during the mixed-fleet window a not-yet-reaccounted node still
  emits `_in6`. If we tighten the regex too early, the old node's `_out` is still read correctly (it's
  the v6 saddr garbage = near-zero) and `_in6` is dropped — billing for that node stays broken until
  reaccounted, which is the pre-fix status quo. Safe. Tighten the regex after the fleet is fully
  reaccounted, or leave `in6` in the alternation harmlessly.
- **per-port vs per-batch cfg naming:** the quota is strictly **per-port** (one nft object per port).
  There is no per-batch quota object — "batch" only exists in the orchestrator's `_ACCOUNTING_PORT_CHUNK`
  HTTP chunking (`node_client.py:97`), which is unrelated to nft and stays 100. Do not conflate. The
  quota-push endpoint is per-port; a future bulk `/accounts/quota` (array body) is a nice-to-have but not
  required (loop the single endpoint; node-agent already loops cheaply).
- **Counter reset on rule replacement:** covered in §4.1 — reset is detected and clamped, no negative
  billing. Same mechanism protects against node reboot / `nft` ruleset reload.
- **nft quota `drop` vs in-flight TCP:** `drop` silently discards over-limit packets; the client's TCP
  stalls and eventually times out rather than getting a clean RST. Acceptable for a hard cap (we *want*
  the transfer to stop). The poll-layer SIGKILL is what actually tears down the session; the quota just
  stops bytes flowing in the meantime.
- **Quota `used` preservation race on topup:** the delete+re-add-with-`used` (§3.2) has a sub-millisecond
  window; do it inside one `nft -f` atomic batch. Even if a packet slips, it's bytes, not correctness —
  the poll layer reconciles.

---

## 6. Test plan

**Unit (node-agent, jest-style next to `accounting.js`):**
- `getCountersForPorts` returns `{bytes_in, bytes_out}` (no `in6`) and that `bytes_out` reflects the
  `_out` counter only. Feed a fake `nft -j` payload.
- `setQuota` (new): add-when-absent, update-preserving-`used` when present, 404 on unknown port.
- `COUNTER_NAME_RE` matches `proxy_42_in` / `proxy_42_out`, rejects `proxy_42_in6` (post-tighten).

**Unit (orchestrator, pytest):**
- `traffic_poll._process_sample`: with `{bytes_in: U, bytes_out: D}`, snapshot grows by `U+D` (or `D`
  if download-only chosen); reset (`delta<0`) clamps to 0.
- `pergb_service._best_effort_set_quota_all`: computes full-budget-per-port, calls `post_set_quota` per
  linked port, tolerates a per-node failure.

**Integration (single node, the headline e2e):**
1. Provision a 1 GB pergb pool, generate 1 port, reaccount it. Confirm nft has `proxy_<port>_in/_out`
   (2-rule client-port set, no `_in6`) and (flag on) `quota_<port> over 1GiB`.
2. From a client, download **2 GB** through the port over a target that forces **IPv4 egress** (the old
   bug's blind spot) — e.g. an IPv4-only host.
3. Assert: `proxy_<port>_out` counter climbs ~1:1 with bytes delivered (this is the core fix — pre-fix
   it stayed ~0). After a poll cycle, `traffic_accounts.bytes_used` tracks delivered bytes 1:1.
4. Assert depletion: when `bytes_used >= 1 GiB`, status flips `depleted`, `post_disable` fires, 3proxy
   SIGKILLed, the rest of the 2 GB download cannot complete. Without the quota: cut happens within one
   poll interval of crossing 1 GiB (small overshoot ≈ bytes in one poll window).
5. **With nft-quota on:** assert the kernel drops the download at **exactly** 1 GiB on that port
   (`nft list quota` shows `used == over`, packets dropped) — cut at the limit, not at the next poll.
   Overshoot ≈ 0 (one packet).
6. Repeat step 2 forcing **IPv6 egress via a non-pinned address** — same 1:1 result (proves we no longer
   depend on the `-e` address).

**Cross-node pool sanity:** 2 GB pool, 1 port on node A + 1 port on node B, push ~1 GB through each.
Assert the SUM in `traffic_accounts.bytes_used` reaches 2 GB and the pool depletes + both ports disable
(proves Layer A still aggregates cross-node after the rule change).

**Migration on a live port:** start a long-running download, run `reaccount` mid-stream, confirm 3proxy
PID is unchanged (session survives), confirm the next poll logs `traffic_counter_reset_detected` once
and then resumes counting from 0 with no negative billing.

---

## 7. Open questions for the operator

1. **Billing direction:** count **upload+download** (current behaviour, conservative for the house) or
   **download-only** (what the customer intuitively "uses")? Affects `traffic_poll.py:253`. Recommend
   keeping upload+download unless product disagrees.
2. **Per-port quota ceiling:** confirm the **full-budget-per-port** backstop (§3.4) is acceptable as a
   loose safety net, vs. wanting a tighter (and product-breaking) `/N` split. Recommend full-budget.
3. **Dual (http+socks) ports:** is the paired http port (`port-10000`) a polled `proxy_inventory` row
   linked to the same `traffic_account`? If not, its download is still uncounted — needs confirming /
   fixing (§5). This is the one open correctness gap outside the core IPv6 fix.
4. **Quota rollout gating:** ship Layer A (metering fix) and Layer B (nft quota) together, or land the
   metering fix first (immediately stops the revenue leak via poll+SIGKILL) and add the kernel quota in a
   follow-up? Recommend: metering fix first (it alone fixes the 0% bug), quota as fast-follow behind the
   config flag.
5. **Reaccount trigger:** orchestrator admin button / scheduled sweep / one-shot ops script for the live
   fleet reaccount? Affects who runs §4.1 step 2 and how it's audited.
6. **Existing mis-billed pools:** users who burned GBs that were never counted — any retroactive
   adjustment, or accept "metering starts now" (recommended; we have no data to reconstruct past usage)?
```
