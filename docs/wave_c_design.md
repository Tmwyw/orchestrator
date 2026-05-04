# Wave C — Telegram Bot Integration Design

Version: 1.0
Date: 2026-05-04
Status: DESIGN PASS COMPLETE — all decisions D1..D7 locked, ready for execution prompts

This document records the architectural agreement for Wave C of the
NETRUN project: standing up `netrun-tg_bot` as the user-facing front
that consumes `netrun-orchestrator` HTTP API for sales and lifecycle
management of IPv6 SOCKS5 proxies (per-piece) and datacenter proxies
(pay-per-GB).

It is the **source of truth** for Wave C implementation. Any deviation
must update this document first.

Decision tracker:

| ID | Topic | Status |
|---|---|---|
| D1 | Port legacy bot vs greenfield rewrite | **DECIDED** — greenfield |
| D2 | Framework choice | **DECIDED** — aiogram v3 |
| D3 | DB schema strategy | **DECIDED** — greenfield, 6 tables aligned with orchestrator-contract |
| D4 | Bot architecture (single vs split) | **DECIDED** — D4.1 single unified process (split-ready), D4.2 monorepo + plain-SQL migrations, D4.3 aiogram lifespan + RedisStorage FSM + uv + structlog |
| D5 | Money model (Decimal contract) | **DECIDED** — Decimal-as-string wire, NUMERIC(18,8) internal, Pydantic v2 validators, ROUND_HALF_EVEN |
| D6 | Notification delivery model | **DECIDED** — poll-based, 1h expiry scan + 1h pergb poll + 5min sweep, aiogram throttling, 75/90/100 pergb thresholds |
| D7 | User migration strategy | **DECIDED** — clean cutover, no legacy data migration |

---

## 1. Goal and non-goals

### Goals

- Deliver Telegram bot front for NETRUN proxy service in repo
  `Tmwyw/netrun_bot` (currently empty), separate from orchestrator and
  node_runtime, per Phase 3 of `docs/roadmap.md`.
- Cover sub-waves C.1..C.10: baseline + CI, HTTP client, atomic
  balance, decimal money model, buy flow, extend flow, notifications,
  pay-per-GB UX, admin commands, legacy cleanup.
- Bot consumes orchestrator over HTTP only (one-way, `X-Netrun-Api-Key`
  header). No shared DB, no Python cross-imports, no orchestrator→bot
  push.

### In scope (C.1..C.10)

- Per-piece IPv6 SOCKS5 purchase: reserve → commit, default 30d
  duration.
- Order extension: extend by N days (calls `/v1/orders/{ref}/extend`).
- Pay-per-GB datacenter tier: reserve + topup (gated on B-8.2 deploy
  in orchestrator).
- User balance + transactions ledger (atomic per D5).
- Order list / history / current proxies download.
- Time-based expiry notifications for per-piece (-3d / -2d / -1d).
- Threshold-based traffic notifications for pay-per-GB (per
  `docs/wave_b8_design.md` § D7 dedup contract).
- Admin commands (subset): stats, lookup user, lookup order, manual
  refund. Subset of orchestrator's `/v1/admin/*`.
- Payment provider integration (provider list deferred to D5 — likely
  crypto-only initially).

### Non-goals (out of scope)

- Multi-currency UI (USD only initially).
- KYC / AML / legal pages (deferred to Wave D).
- Categories / subcategories product tree (legacy had it; not our
  business model).
- Third-party proxy resale (suppliers — legacy had it; not our model).
- Promocodes / referral codes (deferred — possibly Wave D or later).
- Mobile-proxy / residential-proxy SKUs (planned beyond Wave C; SKUs
  in orchestrator only support `ipv6_*` and pay-per-GB DC tiers).
- Channel-subscription gating before purchase.
- Bulk supplier-upload UI.
- Self-service return / refund flow (admin-driven only initially).
- In-bot support chat (deferred — links to external `@support_handle`).
- Horizontal scaling of bot itself (deferred to Wave E+ — gated on
  actual load).

### Constraints

- Bot work begins only after orchestrator Wave B-7b is complete
  (logging, metrics, nginx ACL all deployed). C.1..C.10 numbering
  assumes this baseline.
- Sub-wave C.8 (pay-per-GB UX) is gated on orchestrator B-8.2 deploy
  (real implementation, not the B-8.1 stubs).
- All money values must follow orchestrator § 6.10 contract: Decimal
  serialized as JSON string, never float.
- Architecture rule (one-way HTTP, no shared state) is non-negotiable
  per Wave B-0 architecture doc.

```
                   (HTTP)              (HTTP)
   netrun-tg_bot ─────────► netrun-orchestrator ─────────► node_runtime × 300
   client                   central                         workers
   (Postgres + Redis        (Postgres + Redis)              (3proxy + nftables)
    for FSM only)
```

Bot's Redis usage is narrow: aiogram FSM storage only (RedisStorage from
aiogram.fsm.storage.redis), separate database number from orchestrator
(`REDIS_URL=redis://localhost:6379/1`). No bot-side caching layer, no
queue / pub-sub. Heavy Redis usage (orchestrator-style reservation TTL,
rate limit state) is not adopted in bot.

---

## 2. Legacy inventory

### Decision (D1): GREENFIELD REWRITE

The legacy bot at `C:\__NETRUN__\NETRUN FINAL\tg_bot\` (predecessor's
unfinished project — DO NOT MODIFY) is kept as a **reference only**.
A new bot is written from scratch in `Tmwyw/netrun_bot`.

### Legacy stats

| Property | Value |
|---|---|
| Location | `C:\__NETRUN__\NETRUN FINAL\tg_bot\` |
| Python files | 110 |
| Total LOC | 53 298 |
| Framework | aiogram v3 |
| DB runtime | PostgreSQL (SQLite only as data-import source) |
| Architecture | Split — `bot.py` (user) + `admin_bot.py` (admin) |
| Tables in `migrate_sqlite_to_postgres.py` priority list | 28+ |
| Comment / log encoding | Heavy cp1251-as-utf8 mojibake throughout |

### Why rewrite (not port)

1. **Different business model.** Legacy implements categories /
   subcategories tree, suppliers (third-party proxy resale),
   promocodes, residential_geos, mobile_proxy SKUs, channel
   subscription gating, supplier upload UI, expense tracking.
   ~70-80% of legacy feature surface is out-of-scope for C.1..C.10.
   Surgically excising 80% while preserving the remainder is slower
   and riskier than a clean minimum from scratch.
2. **API client fundamentally incompatible.** Legacy
   `services/proxy_api.py` and `services/proxy_api_v2.py` call an
   external login/cookie auth API at `EXTERNAL_SERVER_URL`
   (predecessor's prior architecture, with endpoints like
   `/api/public/servers`, `/api/public/clients`,
   `/api/public/generate-proxy`). Our orchestrator uses
   `X-Netrun-Api-Key` header against a Pydantic v2 contract. Full
   rewrite of HTTP layer is mandatory under any scenario.
3. **Schema mismatch.** Legacy persists 28+ tables; C.1..C.10 needs
   ~6 (users, balances, orders_local, transactions,
   notifications_state, payment_settings — exact list TBD in D3).
   Greenfield 6 tables aligned with orchestrator-contract is faster
   than 22 deletions plus reshape of the remaining 6.
4. **Mojibake recovery is a code smell.** Legacy
   `db/database.py:_fix_utf8_mojibake()` performs runtime cp1251
   recovery on every text column read, masking encoding bugs in
   writes. A clean rewrite has no need for it.

### Keep as REFERENCE (study, do not port verbatim)

- **UX flow patterns**: keyboard layouts (`handlers/keyboards.py`),
  `callback_data` conventions, FSM state structures for multi-step
  purchase flows. New code learns vocabulary from these.
- **aiogram v3 idioms**: router setup (`bot.py:48-95`), middleware
  patterns, filter chains. Saves 1-2 days of bootstrapping.
- **Payment provider integration**: if predecessor wired a specific
  crypto API (`services/crypto_exchange.py`,
  `services/exchange_client.py`) — selective port of business logic
  after D5 fixes the provider list.

### Hard REWRITE (no inheritance from legacy)

- **Schema** — greenfield per D3, aligned with orchestrator-contract.
- **API client** — new `orchestrator_client.py`, Pydantic models
  reused directly from `orchestrator/api_schemas.py` (vendored or
  imported via shared package — TBD in § 6).
- **All handlers** — different business model, different flows.
- **`Database` mixin classes** (`UsersRepositoryMixin`,
  `OrdersRepositoryMixin`, etc.) — replaced by plain query
  functions or thin per-aggregate modules; no diamond inheritance.
- **`_fix_utf8_mojibake()` recovery** — drop entirely; rely on
  Postgres UTF-8 default and clean writes.

### Out-of-scope modules — DO NOT REFERENCE

These modules implement features not in the C.1..C.10 roadmap.
Fresh Claude in C-prompts must not pattern-match against them:

- `handlers/admin_products*.py` — categories / subcategories tree
- `handlers/promocodes_*.py` — promocode UI + admin
- `handlers/suppliers_*.py`, `handlers/supplier_upload_handlers.py`,
  `handlers/supplier_user_handlers.py` — third-party resale
- `handlers/purchase_mobile.py`, `handlers/purchase_residential.py`
  — mobile / residential SKUs
- `handlers/admin_settings_mobile_*.py`,
  `handlers/admin_settings_geo.py` — mobile config + geo lists
- `handlers/admin_settings_channels.py` — channel-subscription gate
- `handlers/expenses_handlers.py`,
  `handlers/expense_categories_handlers.py` — internal accounting
- `handlers/return_handlers.py` — self-service returns
- `handlers/support_flow.py` — in-bot support chat
- `services/channel_subscription.py`, `services/mobile_proxy_*.py`,
  `services/promocodes_manager.py`, `services/suppliers_manager.py`,
  `services/server_checker.py`, `services/daily_stats_broadcast.py`,
  `services/backup_manager.py`

This shared vocabulary is the contract between this design doc and
fresh Claude in subsequent C-prompts: legacy modules listed under
"Keep as REFERENCE" can be cited as `see legacy bot.py:48-95`;
modules listed under "Out-of-scope" must be ignored entirely.

---

## 3. Bot architecture

### Framework (D2): aiogram v3

Decision (D2): **aiogram v3**. Locked.

Rationale:

- Async-native (`asyncio` first-class), not the sync+async hybrid of
  python-telegram-bot.
- FSM built-in (`StatesGroup`, `State`, dispatcher-scoped context) —
  required for multi-step buy / topup flows.
- Router-based decomposition (`Dispatcher.include_router`) — fits a
  potential split user/admin process model (D4).
- Pydantic-friendly: `aiogram.types.*` are Pydantic v2 models,
  conversion to/from `orchestrator/api_schemas.py` is friction-free.
- Matches the legacy bot's framework — UX patterns (keyboards, FSM
  states) can be referenced verbatim without translating between
  framework dialects.
- Active maintenance, current major (v3.x stable since 2024).

Rejected:

- **python-telegram-bot** — older, sync+async hybrid, worse fit for a
  high-throughput shopping bot.
- **telethon** — MTProto-level low-level client, no built-in handlers
  / FSM. Overkill for a shopping bot.

### Process model (D4.1): SINGLE unified bot — split-ready code structure

**Decision (D4.1)**: ONE bot process, ONE Telegram bot token, ONE
Dispatcher with multiple Routers (user_router + admin_router) gated by
middleware. Locked.

#### Why single (not split)

- Wave C admin scope is small (~6–8 commands: stats, lookup user,
  lookup order, manual refund, cancel order, admin promote-revoke).
  Code-level Router isolation handles separation cleanly. Legacy split
  (`bot.py` + `admin_bot.py`) made sense when admin surface had ~30+
  handlers covering suppliers / promocodes / expenses — all out of
  scope here.
- Single systemd unit, single log stream, single DB pool, single HTTP
  client. Sweep jobs (saga recovery, notification dispatch) live in
  one process — no cross-process coordination.
- Defense-in-depth via `AdminMiddleware` checks `is_admin(user_id)`
  before any admin handler executes.
- Single token compromise mitigated by Telegram-side token rotation +
  `is_admin` middleware (token leak alone does not grant admin without
  a compromised admin's telegram_id).

#### Structural rule — split-ready codebase (locked)

Codebase MUST be structured so that future split into separate
user/admin processes (Wave D hardening if needed) is **mechanical, not
architectural**. Specifically:

- `bot/handlers/user/` — all user-facing routers. MUST NEVER import
  from `bot/handlers/admin/`.
- `bot/handlers/admin/` — all admin-facing routers. Isolated from user
  handlers.
- Shared code — `bot/db/`, `bot/services/orchestrator_client.py`,
  `bot/models/`, `bot/middleware/` — both sides import as peers, not
  cross-domain.
- **No shared in-memory state** between user and admin flows.
  Communication only through DB and orchestrator API.
- `AdminMiddleware` (the `is_admin` gate) — single point of truth for
  admin authorization. Applied ONLY to `admin_router` via
  `dispatcher.include_router(admin_router, ...)` with middleware
  registration on that router specifically (not on the global
  Dispatcher).

#### Future split migration path (if needed in Wave D)

1. Create second entrypoint `bot/admin_main.py` (mirror of
   `bot/main.py`).
2. `admin_main.py` initialises a SEPARATE Telegram bot token (env
   `ADMIN_BOT_TOKEN`).
3. `admin_main.py` registers ONLY `admin_router` (not `user_router`).
4. Deploy second systemd unit `netrun-admin-bot.service`.
5. `main.py` removes `admin_router` include.

Migration touches only entrypoint files + systemd config — zero
changes in `handlers/`, `services/`, `db/`. Reversible.

This is pre-paid optionality: cost zero now (clean code structure is
required regardless), benefit large if it ever becomes needed.

### Repo layout (D4.2): MONOREPO `Tmwyw/netrun_bot`

**Decision (D4.2)**: single Git repo, monorepo. Process split (D4.1)
and repo split are orthogonal — future user/admin process split lives
inside this same repo via two entrypoints (`bot/main.py` +
`bot/admin_main.py`) sharing the same Docker image, deps, and code.

#### Why monorepo (not split repos)

- Single team (solo operator + 1–2 trusted admins) → single owner
  surface
- Shared code (`db/`, `services/`, `models/`) is the dominant code
  mass — splitting would force a third "shared" repo + version
  coordination
- Unified versioning is correct: both processes ship same deps, same
  Pydantic models, same DB schema
- One CI pipeline, one PR review surface, one Docker image build

#### Top-level layout

```
netrun_bot/                              # repo root
├── bot/                                 # application code
│   ├── __init__.py
│   ├── main.py                          # entrypoint (single unified bot process)
│   ├── migrate.py                       # plain-SQL migration runner (mirror orchestrator)
│   ├── config.py                        # pydantic-settings env loader
│   ├── handlers/
│   │   ├── __init__.py
│   │   ├── user/                        # USER-FACING — must not import bot.handlers.admin
│   │   │   ├── __init__.py
│   │   │   ├── start.py                 # /start, registration, language
│   │   │   ├── balance.py               # /balance, /history
│   │   │   ├── buy.py                   # FSM buy flow (per-piece)
│   │   │   ├── orders.py                # /orders, /proxies download
│   │   │   ├── extend.py                # /extend flow
│   │   │   ├── topup.py                 # /topup + payment provider callbacks
│   │   │   └── pergb.py                 # pay-per-GB UX (sub-wave C.8)
│   │   └── admin/                       # ADMIN-FACING — must not import bot.handlers.user
│   │       ├── __init__.py
│   │       ├── stats.py
│   │       ├── lookup.py
│   │       ├── refund.py
│   │       ├── cancel.py
│   │       └── manage_admins.py         # super_admin only
│   ├── middleware/
│   │   ├── __init__.py
│   │   ├── logging.py                   # structured per-update logging
│   │   ├── ensure_user.py               # auto-register, bump last_seen_at
│   │   └── admin_gate.py                # is_admin check, applied to admin_router only
│   ├── db/                              # repository layer (one module per table per § 4)
│   │   ├── __init__.py
│   │   ├── pool.py                      # asyncpg pool lifecycle
│   │   ├── users.py
│   │   ├── balances.py
│   │   ├── transactions.py
│   │   ├── orders_local.py
│   │   ├── notifications_state.py
│   │   └── administrators.py
│   ├── services/                        # business logic + async-loop-driven workers
│   │   ├── __init__.py
│   │   ├── users.py                     # normalize_lang, ensure_user orchestration
│   │   ├── orchestrator_client.py       # httpx wrapper + Pydantic schemas
│   │   ├── saga.py                      # saga step orchestration
│   │   ├── sweep.py                     # saga recovery + stale pending cleanup
│   │   ├── notifier.py                  # atomic claim + Telegram send + retries
│   │   └── scheduler.py                 # periodic loops (expiry scan, pergb poll)
│   ├── models/
│   │   ├── __init__.py
│   │   ├── orchestrator.py              # vendored from orchestrator/api_schemas.py
│   │   └── domain.py                    # bot-internal types
│   └── keyboards/
│       ├── __init__.py
│       ├── user.py
│       └── admin.py
├── migrations/                          # PLAIN SQL — mirror of orchestrator's pattern
│   └── 0001_initial_schema.sql          # full § 4 DDL as one revision
├── scripts/
│   ├── bootstrap_admin.py               # § 4 bootstrap procedure
│   └── reconcile.py                     # ledger drift detection (sub-wave C.10)
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── unit/                            # mirrors bot/ structure
│   └── integration/                     # full saga flows, real Postgres, mocked orchestrator
│       ├── test_saga_purchase.py
│       ├── test_saga_refund.py
│       └── test_notifications.py
├── .env.example
├── .github/workflows/ci.yml
├── .gitignore
├── pyproject.toml                       # deps + tool config
├── uv.lock                              # OR poetry.lock — D4.3 picks
├── README.md
└── docker-compose.yml                   # local dev: postgres + bot
```

#### Layer responsibilities

| Layer | Owns | Imports from |
|---|---|---|
| `handlers/` | aiogram routers, FSM definitions, message rendering, callback routing | services, keyboards, models |
| `middleware/` | aiogram middleware (logging, auth gate, ensure_user) | services, db |
| `services/` | business logic (saga, notifier, scheduler), orchestration of db + external | db, models |
| `db/` | data access — repository pattern per table, atomic helpers | models (Pydantic types only) |
| `models/` | Pydantic types — domain + orchestrator wire format | nothing (leaf layer) |
| `keyboards/` | InlineKeyboard layouts, callback_data conventions | models (type hints only) |

**Import direction rule**: handlers → services → db → models. Never
reverse. No cross-imports between `bot/handlers/user/` and
`bot/handlers/admin/`. Enforced via `import-linter` config (D4.3 dev
tooling).

#### Migrations: plain SQL — mirror of orchestrator

Mirrors `orchestrator/migrate.py` and `migrations/{NNN}_{description}.sql`
exactly. Operator already owns this pattern from orchestrator —
re-using it across repos eliminates context switching.

- File naming: `migrations/{NNN}_{description}.sql`, 3-digit
  zero-padded version (e.g., `0001_initial_schema.sql`,
  `0002_pergb_parent_order_ref.sql`).
- Tracking: `schema_migrations(version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT now())` table, created on
  first run.
- Runner: `bot/migrate.py` — semantic mirror of
  `orchestrator/migrate.py`. Sorted glob over `migrations/*.sql`,
  skip if already in `schema_migrations`, apply + INSERT
  tracking row.
- Driver: asyncpg (single DB driver across bot codebase) — small
  divergence from orchestrator's psycopg3 sync runner. Bot's
  `migrate.py` wraps in `asyncio.run(...)` for sync CLI feel.
- Entrypoint: `python -m bot.migrate` runs pending migrations.
  Invoked manually before bot start, or as systemd unit
  pre-start hook.
- C.1 deliverable: `0001_initial_schema.sql` containing the full §
  4 DDL as one transaction.
- Forward-only by convention. Reverse SQL crafted manually if ever
  needed (rare in v1).

Trade-off vs alembic explicitly accepted: bot DB scope is small (6
tables, 1–2 migrations per year expected after C.1), alembic's
autogenerate / branch-merge / down-migration machinery is unjustified
overhead. If bot DB ever grows past ~30 tables with weekly schema
churn → switch to alembic with `alembic stamp` on the current SQL
revision. Reversible.

#### Orchestrator schema vendoring

`bot/models/orchestrator.py` is **vendored** from
`orchestrator/api_schemas.py`. Each vendored copy carries a header
docstring pinning the source commit:

```python
"""Vendored from Tmwyw/orchestrator commit 89c982b (2026-04-29).

Update workflow:
1. Bump orchestrator commit hash in this docstring.
2. Re-copy api_schemas.py from orchestrator at that commit.
3. Run integration tests against orchestrator-staging.
4. Commit + version bump in pyproject.toml.
"""
```

Drift mitigated by integration tests in CI that hit real orchestrator
endpoints (response shape mismatch → test fail). If orchestrator
schema churn becomes frequent → migrate to OpenAPI auto-generation
(forward-compat note in § 10.B).

#### Tests integration DB

- CI: `pytest-postgresql` spawns ephemeral Postgres per test session.
  Fast, isolated.
- Local dev: `docker-compose.yml` with postgres + bot services.
  Standard local workflow.
- Trade-off accepted: two test DB strategies (CI vs local) is
  standard split.

#### Background tasks placement

`sweep` / `scheduler` / `notifier` live in `services/` — they are
business-logic services that happen to be async-loop-driven. They are
spawned from `bot/main.py` lifespan via `asyncio.create_task` (D4.3
detail). Future scale (Wave D): if single-process bot stops handling
load, extract sweepers/schedulers to separate worker processes
without touching service module structure.

#### Entrypoint naming through future split (D4.1 cross-reference)

`bot/main.py` is the current single-process entrypoint. Future split
(per D4.1 migration path) creates `bot/admin_main.py`. After split,
`main.py` remains user-facing — the rename is intentionally avoided
to preserve git history continuity and minimize churn.

### Async patterns + entrypoint structure (D4.3)

**Decision (D4.3)**: locked across 8 sub-decisions covering lifespan,
DB driver, HTTP client, FSM storage, build system, logging, config,
and background task supervision.

#### Lifespan via Dispatcher startup/shutdown decorators

aiogram v3's `dp.startup()` / `dp.shutdown()` decorators own resource
lifecycle. Pool, orchestrator client, and background tasks are bound
to the Dispatcher's lifecycle:

```python
@dp.startup()
async def on_startup(dp: Dispatcher) -> None:
    pool = await asyncpg.create_pool(
        dsn=str(settings.database_url),
        min_size=settings.bot_db_pool_min_size,
        max_size=settings.bot_db_pool_max_size,
        command_timeout=30.0,
    )
    orchestrator_client = OrchestratorClient(
        base_url=str(settings.orchestrator_base_url),
        api_key=settings.orchestrator_api_key,
    )
    dp["pool"] = pool
    dp["orchestrator_client"] = orchestrator_client
    dp["sweep_task"] = asyncio.create_task(
        supervised(lambda: sweep_loop(pool, orchestrator_client, bot),
                   name="sweep")
    )
    dp["scheduler_task"] = asyncio.create_task(
        supervised(lambda: scheduler_loop(pool, orchestrator_client, bot),
                   name="scheduler")
    )

@dp.shutdown()
async def on_shutdown(dp: Dispatcher) -> None:
    dp["sweep_task"].cancel()
    dp["scheduler_task"].cancel()
    await asyncio.gather(
        dp["sweep_task"], dp["scheduler_task"],
        return_exceptions=True,
    )
    await dp["orchestrator_client"].close()
    await dp["pool"].close()
```

Dependencies are passed to handlers via `dp["..."]` data injection.
aiogram resolves them as keyword arguments in handler signatures.

#### asyncpg pool

- `min_size=2`, `max_size=10` defaults (env-tunable via
  `BOT_DB_POOL_MIN_SIZE` / `BOT_DB_POOL_MAX_SIZE`).
- `command_timeout=30.0` — runaway-query safety net.
- Per-call acquire pattern: `async with pool.acquire() as conn,
  conn.transaction():` inside repository methods. Explicit transaction
  boundaries.

#### httpx AsyncClient — single process-scoped instance

```python
class OrchestratorClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"X-Netrun-Api-Key": api_key},
        )

    async def reserve(...) -> ReserveResponse: ...
    async def commit(...) -> CommitResponse: ...
    async def get_traffic(...) -> TrafficResponse: ...

    async def close(self) -> None:
        await self._client.aclose()
```

Single AsyncClient lives for the bot process — connection pool reuse,
fewer TCP handshakes. Retry policy lives in service methods (per saga
step 8: exponential backoff for 5xx/timeout, no retry for 4xx, max 5
attempts).

#### FSM storage: RedisStorage

```python
from aiogram.fsm.storage.redis import RedisStorage

storage = RedisStorage.from_url(str(settings.redis_url))
dp = Dispatcher(storage=storage)
```

Why Redis (not MemoryStorage):
- Bot crash mid-flow loses FSM state with MemoryStorage → user stuck
  in broken half-complete buy / topup / extend flow
- Redis is already deployed for orchestrator at the host level — bot
  uses a separate database number (e.g. `REDIS_URL=redis://localhost:6379/1`)
  to keep keyspaces disjoint
- Cost minimal — no new infrastructure; one extra connection from the
  bot process

(Updates § 1 architecture diagram annotation: bot stack is
"Postgres + Redis for FSM only".)

#### Build system: uv

- `uv init` + `pyproject.toml` + `uv.lock`
- Faster install vs poetry (~10× cold cache)
- Single tool for python install + venv + lockfile
- Forward-compat: switch to poetry in Wave D if operator preference
  shifts

#### Logging: structlog with JSON renderer

- Mirrors orchestrator's B-7b.1 logging pattern
- `bot/logging_setup.py` configures structlog at startup
- All modules use `logger = structlog.get_logger("bot.<module>")`
- JSON output for log aggregation in production

#### Config: pydantic-settings

Single `Settings` class in `bot/config.py`:

```python
from pydantic import HttpUrl, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    bot_token: str
    database_url: PostgresDsn
    redis_url: RedisDsn
    orchestrator_base_url: HttpUrl
    orchestrator_api_key: str

    bot_db_pool_min_size: int = 2
    bot_db_pool_max_size: int = 10
    stale_threshold_hours: int = 6        # per orders_local mirror staleness
    sweep_interval_sec: int = 300         # 5-min sweep cycles (saga + notifier)
    scheduler_expiry_interval_sec: int = 3600  # 1-hour expiry scan
    bootstrap_telegram_id: int | None = None   # used once at first deploy
```

All configuration env-driven; production via systemd unit env or
Docker. Pydantic types catch misconfigs at startup, not at runtime.

#### Background task supervision

Bare `asyncio.create_task(coro())` silently dies on unhandled
exception. Wrap in supervisor:

```python
async def supervised(coro_factory, name: str) -> None:
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("background_task_crashed", task=name)
            await asyncio.sleep(5)  # backoff before restart
```

Both `sweep_loop` and `scheduler_loop` run under `supervised`.
Crash → log structured event → 5-second backoff → resume.

#### Dev tooling

| Tool | Purpose |
|---|---|
| **ruff** | Lint + format (replaces black, isort, flake8, pyupgrade) |
| **mypy --strict** | Type checking on `bot/` (tests looser if needed) |
| **import-linter** | Enforces D4.1 split-rule + D4.2 layer direction (CI) |
| **pytest** + pytest-asyncio + pytest-postgresql + respx | Tests |
| **pre-commit** | ruff format + ruff check + mypy + import-linter on staged |
| **GitHub Actions** | CI pipeline matching `Tmwyw/netrun_bot` repo location |

`import-linter` config in `pyproject.toml`:

```toml
[tool.importlinter]
root_package = "bot"

[[tool.importlinter.contracts]]
name = "user handlers must not import admin handlers"
type = "forbidden"
source_modules = ["bot.handlers.user"]
forbidden_modules = ["bot.handlers.admin"]

[[tool.importlinter.contracts]]
name = "admin handlers must not import user handlers"
type = "forbidden"
source_modules = ["bot.handlers.admin"]
forbidden_modules = ["bot.handlers.user"]

[[tool.importlinter.contracts]]
name = "layer dependency direction"
type = "layers"
layers = [
    "bot.handlers",
    "bot.middleware",
    "bot.services",
    "bot.db",
    "bot.models",
]
```

CI step `lint-imports` exits non-zero on violation.

---

## 4. DB schema

### Decision (D3): GREENFIELD — 6 tables aligned with orchestrator-contract

Per D1 (greenfield rewrite) and D7 (no legacy migration), bot starts with
empty DB. Schema is purpose-built for C.1..C.10 scope, not a translation
of legacy 28+ tables.

Six tables:

1. `users` — bot identity / Telegram metadata (bot-owned, root)
2. `balances` — money source-of-truth bot-side, hot row per user (bot-owned)
3. `transactions` — append-only money ledger (bot-owned, audit forever)
4. `orders_local` — eventually-consistent mirror of orchestrator orders
5. `notifications_state` — Telegram delivery dedup + tracking (bot-owned)
6. `administrators` — admin authorization + audit (bot-owned)

### Cross-table policies

**Money type contract**: All money columns use `NUMERIC(18,8)`. Mirrors
orchestrator's pattern (per `docs/wave_b8_design.md`). Supports crypto
precision (BTC up to 8 decimals, USDT typically 6, fiat 2). Never
`float`/`real`/`double precision`. Decimal as JSON string at HTTP
boundary per orchestrator § 6.10 contract.

**Timestamp contract**: All timestamps `TIMESTAMPTZ NOT NULL DEFAULT
now()` unless explicitly nullable for "not yet happened" semantics
(`committed_at`, `revoked_at`, `delivered_at`, `last_seen_at`,
`last_synced_at`). UTC stored, application converts to user TZ for UI
(deferred — single TZ in v1).

**Telegram ID type**: `BIGINT` — Telegram uses int64 user IDs.

**Order ref type**: `TEXT` — orchestrator's order_ref format. Cross-DB
references to orchestrator entities are `TEXT` without FK (different
database). Integrity maintained by saga + reconciliation jobs.

**ON DELETE RESTRICT immortality**: All FKs use `ON DELETE RESTRICT`.
Combined effect: hard-delete of users blocked while balances /
transactions / orders_local / administrators rows reference them.
Soft-delete only via `users.status = 'banned'`. Money / audit trail
preserved forever. Storage cost negligible at projected scale; the
compliance / audit trade-off dominates.

**Status enums use TEXT + CHECK**: All status fields are
`TEXT NOT NULL CHECK (status IN (...))`. ALTER-friendly as new statuses
arrive (vs Postgres `CREATE TYPE ... AS ENUM` which requires `ALTER
TYPE` and is global). Performance trade-off (text comparison vs enum
ord) irrelevant at scale.

**App-level invariants** (not DB-enforced; documented for repository
layer + test coverage):

- `users` ↔ `balances` are 1:1 — balance row created in same DB tx as
  user row in `bot/services/users.py:ensure_user`.
- `users.updated_at` bumps only on changes to telegram_id / username /
  first_name / last_name / language_code / status. `last_seen_at` is
  independent — bumped on every interaction without affecting
  `updated_at`.
- `language_code` normalization — Telegram sends arbitrary 2–3 letter
  codes, bot maps unsupported → `'en'` via
  `bot/services/users.py:normalize_lang(raw) -> 'en' | 'ru'`.
- `users.status='frozen'` is preemptively included in CHECK enum but
  unused in v1; reserved for Wave D.5 KYC/AML state. Adding it now
  avoids blocking ALTER on a live DB later.
- `transactions` is append-only — no UPDATE, no DELETE. Invariant +
  test coverage. DB-level REVOKE deferred to Wave D operational
  hardening.
- `transactions` sign-by-kind enforced via DB CHECK constraint (money
  bugs catastrophic).
- `transactions.idempotency_key` is required NOT NULL UNIQUE for every
  row, including admin-initiated. Application generates UUID for
  admin operations.
- `orders_local` tier-specific column population — application-enforced
  via Pydantic validation in `bot/db/orders_local.py`.
  `tier='ipv6_per_piece'` → `proxy_count`+`duration_days` populated,
  `gb_allowance_snapshot` NULL. `tier='pay_per_gb'` → inverse.
- `orders_local` status transitions — application-enforced via
  repository layer. DB CHECK constrains only the value set, not
  transition validity. State-machine via triggers — overkill for v1.
- `notifications_state` atomic claim — `INSERT ... ON CONFLICT DO
  NOTHING RETURNING id` pattern. Race-free dedup. Documented in § 8.

### Concurrency contract for money flow

Pessimistic row-level locks via `SELECT ... FOR UPDATE`. Standard atomic
update path:

```python
async with db.transaction():
    bal = await db.fetchrow(
        "SELECT balance FROM balances WHERE user_id = $1 FOR UPDATE",
        user_id,
    )
    new_balance = bal["balance"] + delta  # Decimal arithmetic
    if new_balance < 0:
        raise InsufficientFunds()
    await db.execute(
        "UPDATE balances SET balance = $1, updated_at = now() "
        "WHERE user_id = $2",
        new_balance, user_id,
    )
    await db.execute(
        "INSERT INTO transactions(user_id, amount, kind, ref, "
        "idempotency_key) VALUES ($1, $2, $3, $4, $5)",
        user_id, delta, kind, ref, idem_key,
    )
```

`balances.balance` is denormalized hot cache; `transactions` ledger is
canonical. Reconciliation invariant:

```
balance(user) ≡ SUM(transactions.amount WHERE user_id = X)
```

Drift detection job (sub-wave C.10 cleanup or Wave D ops) scans for
mismatches and inserts `kind='adjustment'` rows + admin alert.

Balance row is immortal (cannot be hard-deleted while transactions
reference user). User soft-delete via `users.status='banned'` does not
remove balance row.

### Concurrent revoke safety (administrators)

Two super_admins simultaneously revoking same admin:

```sql
UPDATE administrators
   SET revoked_at = now(), revoked_by = $me
 WHERE user_id = $target AND revoked_at IS NULL;
```

First wins (1 row affected). Second sees 0 rows — application returns
"already revoked by $first_admin at $first_time". No explicit lock or
constraint required.

### Re-grant flow (administrators)

Re-granting after revoke uses single-row UPDATE:

```sql
UPDATE administrators
   SET role        = $new_role,
       granted_at  = now(),
       granted_by  = $new_granter_id,
       revoked_at  = NULL,
       revoked_by  = NULL,
       notes       = $new_notes
 WHERE user_id = $user_id;
```

Implication: original grant date and revocation context are lost on
re-grant. Single-row simplicity sufficient for v1. Forward-compat:
composite PK `(user_id, granted_at)` or extracted `admin_grants_history`
audit table when admin team scales (§ 10).

### Bootstrap procedure (sub-wave C.1 deliverable)

`granted_by = NULL` is legitimate exclusively for the bootstrap
administrator. All subsequent grants enforce NOT NULL via
`admin_repository.grant(target_user_id, role, granted_by_user_id, notes)`.

Operator runs `scripts/bootstrap_admin.py` once at first deployment:

1. Reads `BOOTSTRAP_TELEGRAM_ID` env var.
2. SELECT/INSERT user with that telegram_id (idempotent).
3. INSERT INTO administrators(user_id, role='super_admin',
   granted_by=NULL, notes='bootstrap') ON CONFLICT DO NOTHING.

Idempotent — re-runs safe. Operator never writes manual SQL — typo risk
reduced.

### Full DDL

```sql
-- =====================================================================
-- Wave C bot schema (greenfield, 6 tables)
-- =====================================================================

-- 1. users — bot identity / Telegram metadata
CREATE TABLE users (
    id              BIGSERIAL    PRIMARY KEY,
    telegram_id     BIGINT       NOT NULL UNIQUE,
    username        TEXT,                                -- @handle, nullable
    first_name      TEXT,
    last_name       TEXT,
    language_code   TEXT         NOT NULL DEFAULT 'en'
                                 CHECK (language_code IN ('en', 'ru')),
    status          TEXT         NOT NULL DEFAULT 'active'
                                 CHECK (status IN ('active', 'banned', 'frozen')),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ
);
-- index on telegram_id implied by UNIQUE constraint

-- 2. balances — money source of truth bot-side, hot row per user
CREATE TABLE balances (
    user_id     BIGINT        PRIMARY KEY
                              REFERENCES users(id) ON DELETE RESTRICT,
    balance     NUMERIC(18,8) NOT NULL DEFAULT 0
                              CHECK (balance >= 0),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 3. transactions — append-only money ledger
CREATE TABLE transactions (
    id                BIGSERIAL     PRIMARY KEY,
    user_id           BIGINT        NOT NULL
                                    REFERENCES users(id) ON DELETE RESTRICT,
    amount            NUMERIC(18,8) NOT NULL CHECK (amount != 0),
    kind              TEXT          NOT NULL
                                    CHECK (kind IN (
                                        'topup',
                                        'purchase',
                                        'extension',
                                        'pergb_topup',
                                        'refund',
                                        'bonus',
                                        'admin_credit',
                                        'admin_debit',
                                        'adjustment'
                                    )),
    ref               TEXT,                                -- polymorphic by kind
    idempotency_key   TEXT          NOT NULL UNIQUE,
    description       TEXT,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- Sign-by-kind enforced at DB level (money bugs catastrophic)
    CONSTRAINT transactions_sign_check CHECK (
        (kind IN ('topup', 'refund', 'bonus', 'admin_credit')
            AND amount > 0)
        OR
        (kind IN ('purchase', 'extension', 'pergb_topup', 'admin_debit')
            AND amount < 0)
        OR
        kind = 'adjustment'  -- signed, admin discretion
    )
);

CREATE INDEX transactions_user_created_idx
    ON transactions(user_id, created_at DESC);

-- 4. orders_local — eventually-consistent mirror of orchestrator orders
CREATE TABLE orders_local (
    id                    BIGSERIAL     PRIMARY KEY,
    user_id               BIGINT        NOT NULL
                                        REFERENCES users(id) ON DELETE RESTRICT,
    order_ref             TEXT          NOT NULL UNIQUE,    -- cross-DB ref
    tier                  TEXT          NOT NULL
                                        CHECK (tier IN ('ipv6_per_piece',
                                                         'pay_per_gb')),
    status                TEXT          NOT NULL
                                        DEFAULT 'pending_orchestrator_commit'
                                        CHECK (status IN (
                                            'pending_orchestrator_commit',
                                            'commit_failed',
                                            'active',
                                            'expired',
                                            'cancelled',
                                            'refunded'
                                        )),
    price_total           NUMERIC(18,8) NOT NULL CHECK (price_total >= 0),

    -- Per-piece-specific (NULL for pay_per_gb)
    proxy_count           INTEGER       CHECK (proxy_count IS NULL
                                               OR proxy_count > 0),
    duration_days         INTEGER       CHECK (duration_days IS NULL
                                               OR duration_days > 0),
    expires_at            TIMESTAMPTZ,

    -- Pay-per-GB-specific (NULL for ipv6_per_piece)
    gb_allowance_snapshot NUMERIC(18,8) CHECK (gb_allowance_snapshot IS NULL
                                               OR gb_allowance_snapshot >= 0),

    -- Saga + sync metadata
    created_at            TIMESTAMPTZ   NOT NULL DEFAULT now(),
    committed_at          TIMESTAMPTZ,
    last_synced_at        TIMESTAMPTZ
);

CREATE INDEX orders_local_user_created_idx
    ON orders_local(user_id, created_at DESC);

CREATE INDEX orders_local_active_expires_idx
    ON orders_local(expires_at)
    WHERE status = 'active' AND tier = 'ipv6_per_piece';

CREATE INDEX orders_local_pending_saga_idx
    ON orders_local(created_at)
    WHERE status = 'pending_orchestrator_commit';

-- 5. notifications_state — Telegram delivery dedup + tracking
CREATE TABLE notifications_state (
    id                    BIGSERIAL    PRIMARY KEY,
    user_id               BIGINT       NOT NULL
                                       REFERENCES users(id) ON DELETE RESTRICT,
    kind                  TEXT         NOT NULL
                                       CHECK (kind IN (
                                           'expiry_reminder',
                                           'commit_failed',
                                           'topup_confirmed',
                                           'pergb_threshold'
                                       )),
    key                   TEXT         NOT NULL,
    delivery_status       TEXT         NOT NULL DEFAULT 'pending'
                                       CHECK (delivery_status IN (
                                           'pending',
                                           'sent',
                                           'failed',
                                           'skipped'
                                       )),
    telegram_message_id   BIGINT,
    attempt_count         INTEGER      NOT NULL DEFAULT 0
                                       CHECK (attempt_count >= 0),
    error_text            TEXT,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    delivered_at          TIMESTAMPTZ,

    CONSTRAINT notifications_state_dedup_uniq
        UNIQUE (user_id, kind, key)
);

CREATE INDEX notifications_state_pending_sweep_idx
    ON notifications_state(created_at)
    WHERE delivery_status = 'pending';

-- 6. administrators — admin authorization + audit
CREATE TABLE administrators (
    user_id      BIGINT       PRIMARY KEY
                              REFERENCES users(id) ON DELETE RESTRICT,
    role         TEXT         NOT NULL
                              CHECK (role IN ('admin', 'super_admin')),
    granted_at   TIMESTAMPTZ  NOT NULL DEFAULT now(),
    granted_by   BIGINT       REFERENCES users(id) ON DELETE RESTRICT,
                              -- NULL legitimate only for bootstrap admin
    revoked_at   TIMESTAMPTZ,
    revoked_by   BIGINT       REFERENCES users(id) ON DELETE RESTRICT,
    notes        TEXT,

    CONSTRAINT administrators_revoke_consistency CHECK (
        (revoked_at IS NULL AND revoked_by IS NULL)
        OR
        (revoked_at IS NOT NULL AND revoked_by IS NOT NULL)
    )
);
```

### Schema-coupled service modules

The repository layer maintains the app-level invariants listed above.
Module map (to be created in sub-wave C.1):

| Module | Owns |
|---|---|
| `bot/db/users.py` | users CRUD + `ensure_user` (atomic with balances row) |
| `bot/db/balances.py` | balance reads, `apply_delta(user_id, delta, kind, ref, idem_key)` atomic helper |
| `bot/db/transactions.py` | ledger inserts, history queries, idempotency-key dedup |
| `bot/db/orders_local.py` | mirror reads/writes, Pydantic tier validators, sweep-job query |
| `bot/db/notifications_state.py` | atomic claim, sweep-job query, status transitions |
| `bot/db/administrators.py` | grant / revoke / re-grant, `is_admin(user_id)` lookup |
| `bot/services/users.py` | `normalize_lang`, ensure_user orchestration |

---

## 5. Money model

**Status**: D5 PARTIAL — saga pattern locked via D3 closeout. Decimal
serialization contract + payment provider integration shape pending
formal D5 prompt.

### Saga pattern (locked) — purchase / extension / pergb_topup flow

External orchestrator HTTP commit cannot live inside a bot DB
transaction. Saga reconciles bot-side money state with
orchestrator-side order state across the HTTP boundary.

```
1.  BEGIN bot tx
2.  SELECT balance FROM balances WHERE user_id = $1 FOR UPDATE
3.  Validate sufficient (Decimal compare against price_total)
4.  UPDATE balances SET balance = balance - price_total,
                          updated_at = now()
        WHERE user_id = $1
5.  INSERT INTO transactions
        (user_id, amount, kind, ref, idempotency_key, description)
    VALUES
        ($1, -price_total,
         'purchase' | 'extension' | 'pergb_topup',
         order_ref, $idem_key, ...)
6.  INSERT INTO orders_local
        (user_id, order_ref, tier, status, price_total,
         proxy_count?, duration_days?, gb_allowance_snapshot?, ...)
    VALUES
        ($1, order_ref, ...,
         'pending_orchestrator_commit',
         price_total, ...)
7.  COMMIT bot tx
8.  POST /v1/orders/{order_ref}/commit
        Headers: X-Netrun-Api-Key, Idempotency-Key: $idem_key
        Retry: transient (5xx, network timeout) — exponential backoff
               up to 5 attempts. Definitive (4xx) — no retry.
9.  On orchestrator commit success:
        BEGIN tx
        UPDATE orders_local
            SET status='active', committed_at=now(), last_synced_at=now()
            WHERE order_ref = $1
        COMMIT
10. On definitive failure (4xx OR retries exhausted):
        BEGIN tx
        INSERT INTO transactions
            (user_id, amount, kind, ref, idempotency_key, description)
        VALUES
            ($1, +price_total, 'refund', order_ref,
             f"refund:{order_ref}",
             'auto-refund: orchestrator commit failed')
        UPDATE balances SET balance = balance + price_total,
                             updated_at = now()
            WHERE user_id = $1
        UPDATE orders_local SET status='commit_failed'
            WHERE order_ref = $1
        COMMIT
        Then: claim notifications_state row (kind='commit_failed',
              key=f"commit_failed:{order_ref}") and notify user.
```

### Saga properties

- **Bot tx commits BEFORE orchestrator commit.** Balance debited even
  if orchestrator unreachable. Trade-off accepted: refund flow handles
  definitive failure; commit retry handles transient failure.
- **Orchestrator commit is idempotent** via `Idempotency-Key` header.
  Bot uses the same key in saga step 5 (`transactions.idempotency_key`)
  and step 8 (HTTP header) — guarantees deduplication on both sides
  during retry storms.
- **Refund uses an ordinary `transactions` row** with `kind='refund'`,
  not a special path. Reconciliation invariant works uniformly.
- **Refund idempotency key** is `f"refund:{order_ref}"` — deterministic.
  Sweep-job retries deduplicate automatically. Assumes max one refund
  per order; partial refunds extend the format to
  `f"refund:{order_ref}:{seq}"` (forward-compat in § 10).
- **User-facing UX**: "balance debited, processing your order" → on
  success → proxies. On definitive failure → refund visible in
  `/balance` history (transactions ledger, `kind='refund'`) with the
  description text.

### Saga recovery (sweep-job)

A bot crash between step 7 and step 8/9/10 leaves
`orders_local.status = 'pending_orchestrator_commit'`. Recovery is an
async task in the bot process (sub-wave C.4–C.5 deliverable):

```sql
SELECT order_ref, user_id, price_total, tier
  FROM orders_local
 WHERE status = 'pending_orchestrator_commit'
   AND created_at < now() - interval '5 minutes'
 ORDER BY created_at ASC
 LIMIT 100;
```

For each row — retry orchestrator commit (saga steps 8–9). On
definitive failure — refund (saga step 10).

The retry's `Idempotency-Key` is reconstructed from the matching
`transactions` row:

```sql
SELECT idempotency_key FROM transactions
 WHERE ref = $order_ref
   AND kind IN ('purchase', 'extension', 'pergb_topup')
 LIMIT 1;
```

Stable identifier — multiple sweep retries are safe.

### Inventory leak protection (orchestrator-side)

Orchestrator's Redis reservation TTL (default 300s, per
`docs/wave_b_design.md`) protects against inventory leaks: if bot
crashed and never reached step 8, orchestrator releases the
reservation by TTL. Bot-side sweep complements this — it recovers
sagas that *did* call orchestrator but lost local consistency between
steps.

### Reconciliation invariant

```
orders_local.price_total
  ≡ -SUM(transactions.amount
         WHERE ref = order_ref
           AND kind IN ('purchase', 'extension', 'pergb_topup'))
```

Soft invariant — spans tables and signs flip. No DB CHECK.
Reconciliation job (sub-wave C.10 cleanup) detects drift, alerts admin,
and inserts a `kind='adjustment'` row to restore consistency.

### Decimal contract (D5)

Wire format, internal representation, rounding policy, and arithmetic
discipline — all locked to eliminate float / Decimal contamination
across the money flow.

#### Wire format with orchestrator

Decimal-as-string per orchestrator's § 6.10 contract. Every money
field on the wire is a JSON string ("501.42" not 501.42). Bot parses
incoming string via `Decimal(value)`. **Never `float()` on a money
value at any step of the saga.**

#### Internal representation

- DB columns: `NUMERIC(18,8)` per § 4 schema (preserves 8 decimal
  places — sufficient for crypto precision).
- Python: `decimal.Decimal` with context precision = 18.
- Pydantic v2 model field type = `Decimal`.

#### Pydantic v2 field shape

```python
from decimal import Decimal
from pydantic import BaseModel, field_validator

class OrderResponse(BaseModel):
    price_amount: Decimal

    @field_validator("price_amount", mode="before")
    @classmethod
    def parse_decimal(cls, v: object) -> Decimal:
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))  # accepts str/int/float, normalizes
```

`mode="before"` validator runs before Pydantic's standard coercion,
catches any non-Decimal input and normalizes via `str()` first
(avoids `Decimal(0.1)` float-binary garbage).

On output, `model_dump(mode="json")` serializes Decimal → str
matching orchestrator wire format.

#### Rounding policy: ROUND_HALF_EVEN (banker's rounding)

Standard for finance — minimizes statistical bias vs HALF_UP.
Configured globally at startup:

```python
# bot/services/money.py
from decimal import getcontext, ROUND_HALF_EVEN

def configure_money() -> None:
    ctx = getcontext()
    ctx.rounding = ROUND_HALF_EVEN
    ctx.prec = 18
```

Called once from `bot/main.py` startup before any Decimal arithmetic.

**Process-wide note**: `getcontext()` modifies the default context for
the entire Python process. Bot has no other Decimal users (orchestrator
schemas vendored as Pydantic models inherit this context too). Safe.
If a future dependency needs different rounding → switch to
`localcontext()` per money operation.

#### Display vs storage format

| Use | Format |
|---|---|
| Storage (DB, in-memory) | `Decimal` at full 8-decimal precision |
| User display, fiat | `quantize(Decimal("0.01"))` → `"$501.42"` |
| User display, crypto | `quantize(Decimal("0.0001"))` → `"0.0019 BTC"` |

`bot/services/money.py:format_money(amount, currency='USD') -> str`
is the only place where quantization happens. **Never quantize for
storage or comparison** — only for rendering.

#### Comparison and arithmetic

- Comparisons: `Decimal vs Decimal` only. Mixing `Decimal` and `float`
  raises `TypeError` in Python 3.x — DB schema + Pydantic types prevent
  this from happening accidentally.
- Saga step 3 sufficiency check: `if balance < amount: raise
  InsufficientFunds()` — both sides Decimal.
- Arithmetic preserves precision: `balance - amount` returns Decimal
  at max precision of operands. No intermediate float conversion.

#### Test fixtures

`tests/conftest.py`:

```python
from decimal import Decimal

def D(value: str) -> Decimal:
    """Test helper for consistent Decimal construction.

    Always pass strings to D() — never floats. D("501.42") not D(501.42).
    The string-only convention catches accidental float construction
    in test code at the call site.
    """
    return Decimal(value)
```

Used throughout tests to ensure consistent Decimal construction.
Float-binary garbage (`Decimal(0.1) → 0.1000000000000000055511151231...`)
is impossible because `D` only accepts `str`.

### Payment provider integration (still partial)

The wire format and internal Decimal handling are locked. Payment
provider specifics (which crypto provider, webhook handling shape,
provider txid → `transactions.ref` mapping) remain partial — to be
nailed down in sub-wave C.5 implementation prompt when provider is
selected.

---

## 6. HTTP client (`bot/services/orchestrator_client.py`)

D4.3 + D5 close the dependency chain — § 6 can now be concrete.

### Shape

`OrchestratorClient` wraps a single process-scoped `httpx.AsyncClient`
and exposes typed methods returning Pydantic models (vendored from
orchestrator per D4.2).

```python
import httpx
from bot.models.orchestrator import (
    ReserveRequest, ReserveResponse,
    CommitRequest, CommitResponse,
    TrafficResponse,
    # ... etc
)

class OrchestratorClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(30.0, connect=5.0),
            headers={"X-Netrun-Api-Key": api_key},
        )

    async def reserve(self, body: ReserveRequest) -> ReserveResponse:
        resp = await self._client.post(
            "/v1/orders/reserve",
            json=body.model_dump(mode="json"),
        )
        resp.raise_for_status()
        return ReserveResponse.model_validate(resp.json())

    async def commit(
        self, order_ref: str, idem_key: str,
    ) -> CommitResponse:
        return await self._with_retries(
            "POST", f"/v1/orders/{order_ref}/commit",
            headers={"Idempotency-Key": idem_key},
            response_model=CommitResponse,
        )

    async def get_traffic(self, order_ref: str) -> TrafficResponse:
        resp = await self._client.get(
            f"/v1/orders/{order_ref}/traffic",
        )
        resp.raise_for_status()
        return TrafficResponse.model_validate(resp.json())

    async def close(self) -> None:
        await self._client.aclose()
```

### Decimal handling

Per D5: every money field on the wire is a JSON string. Vendored
Pydantic models in `bot/models/orchestrator.py` declare these as
`Decimal` with the `parse_decimal` `field_validator` shown in § 5.
`model_dump(mode="json")` serializes back to string. No float ever
crosses the boundary.

### Retry policy (saga step 8)

Internal `_with_retries` helper applies the saga step 8 contract:

- **Transient failures** (5xx status, `httpx.TimeoutException`,
  `httpx.NetworkError`): exponential backoff
  (1s → 2s → 4s → 8s → 16s), max 5 attempts.
- **Definitive failures** (4xx status): no retry, raise immediately
  for saga step 10 refund flow.
- **2xx**: parse + return.

Same `Idempotency-Key` header carried across all retries — orchestrator
deduplicates on its side.

### Schema vendoring

`bot/models/orchestrator.py` vendored from
`orchestrator/api_schemas.py` per D4.2. Header docstring pins source
commit hash; update workflow documented inline. Drift caught by
integration tests against orchestrator-staging in CI. Auto-generation
from `/openapi.json` is forward-compat (§ 10.B).

### Required orchestrator endpoints

Bot uses the following orchestrator endpoints (verify presence before
each sub-wave that needs them):

| Endpoint | Purpose | Sub-wave |
|---|---|---|
| `POST /v1/orders/reserve` | Saga step 1 — get order_ref | C.5 |
| `POST /v1/orders/{ref}/commit` | Saga step 8 — confirm order | C.5 |
| `POST /v1/orders/{ref}/extend` | Extension flow | C.6 |
| `GET /v1/orders/{ref}` | Sync mirror | C.5 (on-action), C.7 (background) |
| `GET /v1/orders/{ref}/traffic` | Pergb threshold scheduler | C.8 |
| `POST /v1/orders/{ref}/topup_pergb` | Pergb topup flow | C.8 |
| `GET /v1/admin/users/{ref}` (or similar) | Admin lookup | C.9 |

Per § 10.E: no `/v1/orders/expiring` or per-user-orders endpoint
visible in `orchestrator/main.py` currently. Notification scheduler
in C.7 reads from local `orders_local` mirror (not orchestrator), so
no orchestrator-side endpoint is strictly required for per-piece
reminders. Pergb threshold scheduler (C.8) needs `GET
/v1/orders/{ref}/traffic` — verify before C.8 starts.

---

## 7. UX flows

D2 (aiogram v3) + D4 (single bot, RedisStorage FSM) + D5 (Decimal
contract) + D6 (poll notifications) all locked — UX flows can be
concrete. Detailed sequence implementations land in C.5 / C.6 / C.7 /
C.8 / C.9 sub-wave prompts; this section pins the contract each
sub-wave must satisfy.

### 7.1 Buy flow (per-piece IPv6 SOCKS5) — sub-wave C.5

FSM states (RedisStorage-persisted across bot restarts):

```
BuyFlow:
  ├─ choosing_count       (user picks N proxies)
  ├─ choosing_duration    (default 30d, optional override)
  ├─ confirming           (price computed, awaiting "OK")
  └─ <terminal — saga executes>
```

Sequence:

```
User                     Bot                      Orchestrator
 │                        │                         │
 │── /buy ───────────────►│                         │
 │                        │ FSM=choosing_count      │
 │◄── inline keyboard ────│                         │
 │── tap "5 pcs" ────────►│                         │
 │                        │ FSM=choosing_duration   │
 │◄── 30d default ────────│                         │
 │── tap "OK" ───────────►│                         │
 │                        │ FSM=confirming          │
 │                        │── POST /reserve ───────►│
 │                        │◄── ReserveResponse ─────│
 │                        │   (price_amount,        │
 │                        │    order_ref)           │
 │◄── price + Confirm ────│                         │
 │── tap "Confirm" ──────►│                         │
 │                        │ <SAGA steps 1-7,        │
 │                        │  bot tx commit>         │
 │                        │── POST /commit ────────►│
 │                        │◄── CommitResponse ──────│
 │                        │ <SAGA step 9>           │
 │◄── proxies + creds ────│                         │
```

On orchestrator commit failure: saga step 10 fires, user receives
`commit_failed` notification with refund visible in `/balance`
history.

### 7.2 Extend flow — sub-wave C.6

FSM states:

```
ExtendFlow:
  ├─ choosing_order       (user picks order from /orders list)
  ├─ choosing_extension   (+7d / +14d / +30d)
  ├─ confirming
  └─ <saga executes — same shape as buy, kind='extension'>
```

Reuses saga pattern from § 5 with `kind='extension'`. New `transactions`
row references same `order_ref`, `orders_local.expires_at` updated to
new expiry on saga step 9 success.

### 7.3 Topup flow — sub-wave C.5

```
User                     Bot                      Provider           Bot Webhook
 │                        │                         │                   │
 │── /topup ─────────────►│                         │                   │
 │◄── amount + provider ──│                         │                   │
 │── tap "$50 USDT" ─────►│                         │                   │
 │                        │── create payment ──────►│                   │
 │                        │◄── payment_id + URL ────│                   │
 │◄── pay link + status ──│                         │                   │
 │── pays externally ────────────────────────────► │                   │
 │                        │                         │── webhook ───────►│
 │                        │                         │                   │ <atomic:
 │                        │                         │                   │  INSERT topup tx,
 │                        │                         │                   │  UPDATE balance>
 │                        │                         │                   │
 │                        │ atomic claim notification (kind='topup_confirmed')│
 │◄── "Balance topped up: $X" ──────────────────────────────────────────│
```

Provider-specific shape (which crypto provider, exact webhook payload)
finalized in C.5 sub-wave prompt — D5 leaves this partial intentionally.

### 7.4 Notifications — sub-wave C.7

Per § 8 atomic claim pattern + D6 cadence. No user-initiated UX —
notifications are bot-initiated background sends. User experience:

- Per-piece: receives "your order expires in 3 days / 2 days / 1 day"
  reminders fired by hourly scheduler (D6.1)
- Pay-per-GB: receives "you've used 75% / 90% / 100% of your GB
  allowance" (D6.2, sub-wave C.8)
- Topup confirmation (kind='topup_confirmed', triggered from § 7.3)
- Commit failure refund notice (kind='commit_failed', triggered from
  saga step 10)

### 7.5 Pay-per-GB UX — sub-wave C.8 (B-8.2 gated)

FSM states for pergb topup:

```
PergbTopupFlow:
  ├─ choosing_gb_amount   (10 GB / 50 GB / 100 GB / custom)
  ├─ confirming           (price computed)
  └─ <saga executes — kind='pergb_topup', new orders_local row>
```

Per D7 wave_b8_design.md, each pergb topup creates a new `orders_local`
row with `tier='pay_per_gb'` (forward-compat schema additions in §
10.A). Orchestrator-side aggregation across topups maintains the
parent pergb account state.

`/orders` view shows pergb topups inline with per-piece orders
chronologically, distinguished by `tier` badge.

### 7.6 Admin commands — sub-wave C.9

Admin commands gated by `AdminMiddleware` (per D4.1) on `admin_router`.
No FSM — admin commands are stateless single-shot:

| Command | Action |
|---|---|
| `/admin stats` | aggregate counts (users, active orders, revenue last 7d) |
| `/admin lookup user @handle` | show user info, balance, recent orders |
| `/admin lookup order ord_xxx` | show order details (mirror + orchestrator fetch) |
| `/admin refund ord_xxx <amount>` | manual refund: INSERT `kind='admin_credit'` tx + UPDATE balance + UPDATE order status |
| `/admin cancel ord_xxx` | cancel active order (calls orchestrator cancel endpoint, updates mirror) |
| `/admin promote @handle [admin\|super_admin]` | super_admin only — INSERT/UPDATE administrators row |
| `/admin revoke @handle` | super_admin only — soft-revoke pattern per § 4 |

### 7.7 Rendering

Notification + UX message rendering uses **Jinja2 templates** in
`bot/templates/{kind}_{language_code}.j2`. Forward-compat for i18n
(Wave D). For v1 with `language_code IN ('en', 'ru')`, two templates
per kind. Templates rendered in `bot/services/notifier.py`.

---

## 8. Notification engine

**Status**: D6 PARTIAL — state-machine + dedup contract locked via D3
closeout. Telegram delivery transport (webhook vs long-polling) pending
formal D6 prompt; both transports are compatible with the locked
contract below.

### Atomic claim pattern (locked)

Race-free dedup via `INSERT ... ON CONFLICT DO NOTHING RETURNING id`:

```python
key = compute_key(kind, ...)  # canonical formats below

row = await db.fetchrow(
    """
    INSERT INTO notifications_state
        (user_id, kind, key, delivery_status, attempt_count)
    VALUES ($1, $2, $3, 'pending', 0)
    ON CONFLICT (user_id, kind, key) DO NOTHING
    RETURNING id
    """,
    user_id, kind, key,
)

if row is None:
    return  # already claimed — dedup achieved, exit silently

try:
    msg = await bot.send_message(chat_id=user.telegram_id, text=...)
    await db.execute(
        """
        UPDATE notifications_state
           SET delivery_status='sent',
               telegram_message_id=$1,
               delivered_at=now(),
               attempt_count=attempt_count+1
         WHERE id=$2
        """,
        msg.message_id, row["id"],
    )
except TransientTelegramError as e:
    await db.execute(
        """
        UPDATE notifications_state
           SET attempt_count=attempt_count+1, error_text=$1
         WHERE id=$2
        """,
        str(e), row["id"],
    )
    # row stays 'pending'; sweep job will retry
except DefinitiveTelegramError as e:
    await db.execute(
        """
        UPDATE notifications_state
           SET delivery_status='failed',
               error_text=$1,
               delivered_at=now(),
               attempt_count=attempt_count+1
         WHERE id=$2
        """,
        str(e), row["id"],
    )
```

### Canonical key formats

`key` is application-constructed per kind: deterministic, stable,
human-readable (debug-friendly).

| kind | key format | example |
|---|---|---|
| `expiry_reminder` | `f"{order_ref}:{days}d:{expires_at_canonical}"` | `ord_abc:3d:2026-06-04T12:00:00+00:00` |
| `commit_failed` | `f"commit_failed:{order_ref}"` | `commit_failed:ord_abc` |
| `topup_confirmed` | `f"topup:{payment_id}"` | `topup:cp_xyz123` |
| `pergb_threshold` | `f"{order_ref}:{percent}"` | `ord_pergb:90` |

**Critical: canonical timestamp format for `expiry_reminder`**:

```python
expires_at_canonical = (
    expires_at.replace(microsecond=0).astimezone(UTC).isoformat()
)
# e.g. '2026-06-04T12:00:00+00:00'
```

UTC, no microseconds, fixed precision. Two scheduler runs computing
keys for the same order MUST produce identical strings. Without the
canonical format, microsecond drift produces distinct keys, both
INSERT successfully → double-notification.

### Extension dedup correctness

When user extends a per-piece order, `expires_at` shifts forward → new
`expires_at_canonical` → distinct key per (3d / 2d / 1d) for the new
expiry → fresh notifications fire. The old key (with original
`expires_at_canonical`) remains in DB in terminal state — no spurious
re-fire.

**Edge case (accepted v1 noise)**: user extends order AFTER scheduler
has claimed a pending reminder for OLD `expires_at` but BEFORE Telegram
send completes — pending row delivers with OLD expiry message ("expires
in 3 days at OLD_DATE"). Slight noise, not data corruption. Wave D may
add a pre-send canary check on `orders_local.expires_at` consistency.

### Double-send risk (accepted v1)

Claim-then-send pattern: row is inserted *before* Telegram API call.
Crash window after successful Telegram send but before the DB UPDATE →
next sweep finds `pending`, retries → user receives a duplicate.

**Trade-off accepted**: alternative (send-then-claim) loses atomic
dedup; multiple parallel sweeps would all send. Claim-then-send protects
the high-frequency case (parallel scheduler races) at the cost of a
rare crash-induced duplicate.

Future hardening: per-chat idempotency tokens. Telegram Bot API does
not natively support send-side dedup; workarounds via inline message
edit + retry exist. Defer to Wave D.

### Sweep job (sub-wave C.7 deliverable)

Sweep is an **async task inside the bot process** — not a separate
systemd unit. Implemented as `asyncio.create_task` with infinite loop
and `await asyncio.sleep(300)` between cycles, or via aiogram's
BackgroundScheduler. Failure mode: bot crash → systemd restart → sweep
resumes naturally.

```sql
SELECT id, user_id, kind, key
  FROM notifications_state
 WHERE delivery_status = 'pending'
   AND created_at < now() - interval '5 minutes'
   AND attempt_count < 5
 ORDER BY created_at ASC
 LIMIT 100;
```

Per row: retry the same send flow (UPDATE attempt_count, try Telegram
API, transition to terminal status on success/failure). After 5
attempts → terminal `failed`, alerts admin.

Linear retry policy v1. No exponential backoff. No `next_retry_at`
column. If 429 (Telegram rate limit) becomes frequent in production →
schema addition (forward-compat § 10).

### Lazy cancellation cleanup

When admin cancels an order, pending notifications for that order are
NOT proactively marked `skipped`. Instead, sweep checks
`orders_local.status` on each retry — if `cancelled` or `refunded`,
transitions notification to `skipped` without calling Telegram:

```python
order = await orders_local.get_by_ref(parse_order_ref_from_key(notif.key))
if order is None or order.status in ("cancelled", "refunded"):
    await db.execute(
        """
        UPDATE notifications_state
           SET delivery_status='skipped', delivered_at=now()
         WHERE id=$1
        """,
        notif.id,
    )
    return
```

Trade-off: slightly stale pending rows survive up to one sweep cycle
(~5 min) post-cancel. Acceptable. Avoids cross-table coordination in
the admin command path.

### Notification scheduler (per-piece expiry)

Periodic task (sub-wave C.7) — runs at
`SCHEDULER_EXPIRY_INTERVAL_SEC=3600` (1-hour cadence). Each cycle scans
three narrow 1-hour windows just before each threshold:

```sql
-- 3-day reminder window
SELECT order_ref, user_id, expires_at FROM orders_local
 WHERE status = 'active' AND tier = 'ipv6_per_piece'
   AND expires_at BETWEEN now() + interval '71 hours'
                      AND now() + interval '72 hours';

-- 2-day reminder window
SELECT order_ref, user_id, expires_at FROM orders_local
 WHERE status = 'active' AND tier = 'ipv6_per_piece'
   AND expires_at BETWEEN now() + interval '47 hours'
                      AND now() + interval '48 hours';

-- 1-day reminder window
SELECT order_ref, user_id, expires_at FROM orders_local
 WHERE status = 'active' AND tier = 'ipv6_per_piece'
   AND expires_at BETWEEN now() + interval '23 hours'
                      AND now() + interval '24 hours';
```

For each row, atomic claim against the matching threshold key.
Existing claims dedup naturally via `(user_id, kind, key)` UNIQUE.

**Accepted v1 trade-off — extended downtime missed-window risk**:
narrow 1-hour windows mean a bot outage longer than 1 hour during
the window for a specific order = that specific reminder is missed
(not retried later when bot recovers). Other reminders for the same
order (different threshold) still fire on their windows. Acceptable
for v1 — degraded UX during rare extended downtime, no data loss.
Wave D may revisit with wide-window + per-row threshold compute
pattern (`expires_at <= 72h` + each row computes which thresholds
should have fired by now and atomic-claims each).

### Pergb threshold scheduler (sub-wave C.8, B-8.2 gated)

Hourly poll (`SCHEDULER_EXPIRY_INTERVAL_SEC=3600` cadence shared with
per-piece scanner) of orchestrator `GET /v1/orders/{ref}/traffic` for
each `status='active' AND tier='pay_per_gb'` order in `orders_local`.
Computes `usage_pct` from response, atomic claim per crossing
threshold:

| Threshold | Key |
|---|---|
| 75% used | `f"{order_ref}:75"` |
| 90% used | `f"{order_ref}:90"` |
| 100% used (allowance exhausted) | `f"{order_ref}:100"` |

Per `docs/wave_b8_design.md` § D7 threshold dedup contract — bot's
`notifications_state` is disjoint from any orchestrator-side
`pergb_threshold_state`. Orchestrator's state is for internal events,
bot's is for Telegram delivery dedup.

### Telegram rate limiting

aiogram v3 built-in throttling middleware:

```python
from aiogram.utils.throttling import ThrottlingMiddleware
dp.update.middleware(ThrottlingMiddleware(rate_limit=1.0))
```

Per-chat 1/sec limit (Telegram Bot API constraint for individual
chats). Global 30/sec is respected by aiogram's internal queue. No
custom token bucket needed for v1.

### Notification grouping during recovery

If sweep finds multiple pending notifications for the same user (rare,
typically only after extended downtime recovery), it sends them
sequentially with the throttle middleware naturally pacing 1/sec per
chat. No bulk-message logic needed.

### Failed delivery alerting

Notification reaching terminal `failed` (5 attempts exhausted) emits:

- `logger.error("notification_delivery_failed", user_id=..., kind=...,
  key=..., last_error=...)` — structured log
- Increment metric `netrun_bot_notification_failed_total` (Prometheus
  counter, exposed via `/metrics` endpoint when monitoring lands)

V1: admin reads `journalctl` manually. Wave D adds Grafana alert on
metric counter for proactive notification.

### Sweep cadence

`SWEEP_INTERVAL_SEC=300` (5 min). Sweep loop covers two responsibilities
in one cycle:

1. **Notification retries** (per § 8 sweep query above)
2. **Saga recovery** (per § 5 saga recovery query — finds
   `orders_local.status='pending_orchestrator_commit'` older than 5
   minutes, retries orchestrator commit or refund flow)

Both run sequentially in the same task to avoid two redundant timer
loops.

### Reconciliation

`notifications_state` has no cross-table invariant — self-contained.
No reconciliation job needed. Admin queries against `key` patterns
suffice for debugging "why did $user not receive $reminder".

### Event sourcing model (D6 locked)

**Locked**: poll-based. Orchestrator does NOT push events to bot. Bot
scheduler scans local DB + polls orchestrator API for thresholds.
Webhook *from orchestrator to bot* explicitly out of scope per Wave
B-0 architecture (one-way HTTP).

Telegram-side ingress (long-poll vs webhook from Telegram → bot) is
a deployment detail, not a contract decision — both transports work
with the state-machine and dedup contract above. Default v1:
long-polling (`dp.start_polling(bot)`). Production may flip to
webhook for HTTPS-fronted deployment without schema or contract
changes.

---

## 9. Migration plan

### User migration (D7): NONE — clean cutover

Decision (D7): **no legacy data is migrated**. Greenfield bot starts
with an empty DB.

**Legacy archive (read-only reference)**:

- Location: `C:\__NETRUN__\TRASH\NETRUN_BACKUPS\ZIPKI NETRUN\NETRUN\NETRUN LEGASY\bot_database.db` (SQLite)
- Snapshot stats: 21 users, 59 transactions, 356 purchases,
  90 pending_payments
- Last activity: 2026-02-10 (frozen ~2.5 months by Wave C kickoff,
  2026-04-29)
- Balance type: REAL / float — precision bugs visible in dump
  (e.g. `501.4152999999998`)

The intermediate code-only tree at
`C:\__NETRUN__\NETRUN FINAL\tg_bot\` is half-migrated, never ran in
production on Postgres, has no `.env` and no runtime data. It is
**not a data source**.

### Why clean cutover

1. **Not critical mass.** 21 legacy users is below the threshold
   where compatibility-shim cost outweighs benefit.
2. **Float → Decimal precision-loss communication overhead.**
   Migrating `501.4152999999998` either rounds (user complains:
   "where are my fractions of a cent?") or preserves the float
   garbage in a clean Decimal ledger. Neither is acceptable.
3. **Different business model + different UX.** Legacy users would
   land in a bot that no longer sells what they previously bought
   (no mobile, no residential, no supplier products). A clean start
   is more honest than a compatibility shim.
4. **2.5-month freeze.** Returning-user rate is low at this point.

### Cancellation policy (Wave C scope)

Order cancellation is **admin-only** in Wave C.1..C.10. Self-service
cancel / refund flow is explicitly out of scope per § 1 non-goals.

- **Cancellation UX in bot**: implemented as admin command in the
  admin handler (sub-wave C.9), not as a user-facing button.
- **User-side flow**: "I want to return my order" → user contacts
  external `@support_handle` → admin manually cancels via admin
  command + issues `admin_credit` refund through ledger.
- **Why deferred**: self-service cancel/refund requires fraud
  detection and AML compliance gates before automation is safe. These
  gates land in Wave D.5+ (KYC scope).

### Bootstrap procedure (sub-wave C.1 deliverable)

`scripts/bootstrap_admin.py` is delivered with sub-wave C.1 baseline.
Full procedure documented in § 4 ("Bootstrap procedure"). One-shot,
idempotent. Operator runs once at first deployment with their
`BOOTSTRAP_TELEGRAM_ID` env var; subsequent admin promotions go
through `/admin promote` command (super_admin only).

### Sub-wave breakdown (C.1..C.10)

**Status**: All design-pass decisions (D1..D7) closed. Sub-wave
breakdown unblocked.

Known dependencies:
- Sub-wave **C.5 (saga + refund flow)** uses D5 Decimal contract; only
  payment provider selection (specific crypto provider) outstanding —
  picked at C.5 prompt time.
- Sub-wave **C.8 (pay-per-GB UX)** is gated on orchestrator **B-8.2**
  deploy (real implementation, not the B-8.1 stubs).

Concrete sub-wave breakdown (deliverables, deps, estimated duration)
to be filled in execution prompt sequence (one C-prompt per sub-wave).

---

## 10. Open questions / future

Forward-compat backlog accumulated through D1..D7. Organized by
category. Each item names the trigger condition + implementation
sketch.

### A. Schema additions (extend without restructuring)

- **Multi-currency** (`balances`, `transactions`): drop `balances` PK,
  add `currency TEXT NOT NULL DEFAULT 'USD' CHECK (currency IN (...))`,
  composite PK `(user_id, currency)`. `transactions` adds matching
  `currency` column. Existing rows auto-flag as USD. Trigger: any
  decision to support EUR / BTC / USDT direct balances. Currently
  USD-only per § 1.
- **Frozen / on-hold balance**: `balances.frozen NUMERIC(18,8) NOT NULL
  DEFAULT 0 CHECK (frozen >= 0 AND frozen <= balance)`,
  `available = balance - frozen`. Trigger: Wave D.5 KYC review holds,
  anti-fraud freezes.
- **Bot-side reservation** (alternative to current debit-at-commit):
  add `balances.reserved NUMERIC(18,8) NOT NULL DEFAULT 0`. Trigger:
  if reservation UX ("$X held for in-flight order") becomes
  user-visible requirement. Currently orchestrator-side reservation
  TTL is sufficient.
- **P2P transfers**: `transactions.counterparty_user_id BIGINT NULL
  REFERENCES users(id)`, new kinds `transfer_out` / `transfer_in`.
  Trigger: P2P balance gifting feature.
- **Partitioning** (`transactions`, `notifications_state`): partition
  by `created_at` monthly/quarterly when row count exceeds ~10M.
  Currently bound to ~10k rows total — not needed.
- **pergb topup linkage** (sub-wave C.8): per
  `docs/wave_b8_design.md` § D6.2, each pergb topup creates a new
  `orders_local` row. Add `orders_local.parent_order_ref TEXT NULL`
  (cross-DB ref to the parent pergb account's first order_ref) and
  status value `'completed'` (terminal — indicates one-shot topup
  applied to parent quota). Migration trivial — `ALTER ADD COLUMN` +
  `ALTER ... CHECK` extension. Land at C.8 implementation, not before.
- **`status_history` audit table** (`orders_local`): granular
  per-transition log when current `status` + `transactions` ledger no
  longer suffices. Defer; current visibility adequate.
- **Proxy credentials snapshot**: `orders_local.proxy_credentials_snapshot
  JSONB` for offline `/proxies` download UX without orchestrator
  round-trip. Defer — security trade-off (sensitive data in bot DB),
  evaluate need first.
- **Admin metadata on orders**: `orders_local.admin_notes JSONB` for
  cancellation reasons, refund context. Defer.
- **Bytes_purchased lifetime invariant** (pergb refund schema): when
  refunding pergb topups, refund quantity must be bounded by remaining
  unused allowance — cannot refund bytes already consumed. Likely
  shape: track `bytes_purchased` (snapshot at topup) + bot reads
  `bytes_used` from orchestrator at refund time, refund proportional
  to `bytes_purchased - bytes_used`. Schema implication: `orders_local`
  may need `bytes_purchased BIGINT NULL` (or repurpose
  `gb_allowance_snapshot`). Formalize at C.8 + admin refund flow.

### B. Operational hardening

- **Granular admin roles**: extend `administrators.role` CHECK with
  `support` (read-only), `finance` (refund/credit only), `auditor`
  (read-only with audit logs). ALTER + CHECK update + per-command
  middleware. Trigger: admin team grows past 3–5.
- **Permission table**: explicit `(role, permission)` mapping table
  if CHECK enum scales badly. Defer.
- **Multi-grant history** (`administrators`): when single-row UPDATE
  re-grant pattern starts losing useful audit. Either composite PK
  `(user_id, granted_at)` + INSERT new row on re-grant, or extracted
  `admin_grants_history` audit table. Migration straightforward.
- **MFA / 2FA for admin commands**: extra column `mfa_secret TEXT` or
  separate table. Defer to Wave D security hardening.
- **Activity log for admin commands**: `admin_actions` audit table
  recording every admin command execution (read-only too). Currently
  money-flow admin ops captured via `transactions.kind='admin_credit'/
  'admin_debit'`; read-only commands not logged. Defer.
- **DB-level append-only enforcement** (`transactions`): REVOKE UPDATE,
  DELETE FROM bot_app_user. Defer to Wave D operational hardening.
- **Telegram 429 retry hardening**: aiogram v3 built-in rate limiter
  (30/sec global, 1/sec per chat) is trusted in v1, and `retry_after`
  from Telegram API responses lands in `error_text` only — sweep
  retries linearly. If 429 becomes frequent in production: add
  `notifications_state.next_retry_at TIMESTAMPTZ`, sweep filter
  `WHERE COALESCE(next_retry_at, created_at) < now() - interval '5m'`.
  Migration trivial.
- **Per-chat send-side idempotency**: hardening for the accepted v1
  double-send risk in claim-then-send pattern. Workarounds via inline
  message edit + retry. Defer to Wave D.
- **Notification preferences per user**: opt-out granularity ("I don't
  want expiry reminders"). Defer to Wave D feature set.
- **Pre-send canary** for `expiry_reminder`: sweep checks
  `orders_local.expires_at` matches the timestamp baked into the
  notification key before sending. Eliminates the "extension between
  claim and send" stale-message edge case. Defer.

### C. Refund / money-flow extensions

- **Refund partial-amount support**: v1 assumes max one refund per
  order (idempotency key `f"refund:{order_ref}"`). Partial refunds
  extend to `f"refund:{order_ref}:{seq}"` where `seq` increments per
  partial refund. Schema-friendly extension — no migration needed,
  only application logic. Trigger: refund operations become
  per-line-item rather than per-order.
- **Reconciliation job** (sub-wave C.10 cleanup or Wave D ops):
  periodic compare `SUM(transactions.amount WHERE user_id = X)` vs
  `balances.balance WHERE user_id = X`. On mismatch — INSERT
  `kind='adjustment'` row + UPDATE balances + admin alert.

### D. Wave D feature backlog

- **KYC / AML / legal pages**: deferred to Wave D. `users.status='frozen'`
  reserved for KYC review state. Likely needs `users.phone_number TEXT`
  added at that point.
- **`is_premium BOOLEAN`** (`users`): Telegram premium flag mirror.
  Defer; no current product use.
- **`phone_number TEXT`** (`users`): for KYC verified contact when
  payment provider requires it. Defer to Wave D.5.
- **Promocodes / referral codes**: deferred. Possibly Wave D.
- **In-bot support chat**: deferred. Links to external `@support_handle`
  in v1.
- **Mobile / residential SKUs**: deferred beyond Wave C. `orders_local.tier`
  CHECK extended with `'mobile_per_piece'` / `'residential_per_piece'`
  when these SKUs land in orchestrator.
- **Bot horizontal scaling**: deferred to Wave E+. Single bot process
  in v1; gated on actual load.
- **Multi-language UI**: `users.language_code` CHECK currently limited
  to `('en', 'ru')`. Extend by ALTER + CHECK update + i18n catalog
  expansion when needed.

### E. Known unknowns / external dependencies

- **Orchestrator endpoint gap**: no `/v1/orders/expiring` or per-user
  orders list endpoint currently visible in `orchestrator/main.py`.
  Notification scheduler in C.7 reads from local `orders_local`
  mirror, so no orchestrator-side endpoint is strictly required for
  per-piece reminders. Pergb threshold scheduler (C.8) needs
  orchestrator's `GET /v1/orders/{ref}/traffic` (per Wave B-8 design)
  — verify before C.8 starts.
- **Legacy-user win-back (D7 forward-compat)**: if a Wave D marketing
  decision pursues legacy-user re-acquisition, it implements as a
  single check in the `/start` handler:

  ```python
  if telegram_id in LEGACY_USER_IDS:
      grant_one_time_bonus(amount=legacy_balance)
  ```

  `LEGACY_USER_IDS` (with last-known balances) can be generated as a
  one-shot SQL dump from the SQLite archive at
  `C:\__NETRUN__\TRASH\NETRUN_BACKUPS\ZIPKI NETRUN\NETRUN\NETRUN LEGASY\bot_database.db`
  if needed. **No separate migration sub-wave is required** — the
  clean-cutover decision in § 9 stays intact.
