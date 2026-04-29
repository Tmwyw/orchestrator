# NETRUN — Project-wide Roadmap

> **Это контекстный pointer-документ**, не план действий «сейчас».
> **Текущая работа**: Phase 2 (Wave B) завершается, остался B-8.
> Wave C (bot) и Wave D (launch) — следующие фазы.

## Бизнес-цель

Сервис продажи прокси через Telegram-бот:

- **Per-piece**: IPv6-прокси по странам (USA, UK, DE, ...) на 30 дней с продлением
- **Pay-per-GB**: datacenter-прокси с биллингом по трафику (1/3/5/10/20/30 GB/мес)
- **Целевая нагрузка**: 20 000+ активных юзеров, 1 000-2 000 одновременных покупок, 300+ нод, 1.5M+ прокси в pool

## Архитектура — 3 независимые роли

```
                        (HTTP)                     (HTTP)
   netrun-tg_bot ─────────► netrun-orchestrator ─────────► node_runtime × 300
   client-side             central                         workers
   (Postgres + Redis)      (Postgres + Redis)              (3proxy + nftables)
```

| Роль | Репо | Сервер | Что делает |
|---|---|---|---|
| `orchestrator` | `Tmwyw/orchestrator` | `95.217.98.125` | inventory pool, orders, refill, validation, watchdog, admin, billing |
| `node_runtime` | `Tmwyw/node_runtime` | `139.84.219.149`, `65.20.80.21`, `65.20.72.62` (3 ноды) | 3proxy + nftables + node-agent на :8085 |
| `tg_bot` | `Tmwyw/netrun_bot` (создаётся в Wave C) | TBD (отдельный сервер) | Telegram UX, баланс, история, оплата, доставка |

**Контракт**: bot → orchestrator → nodes (одностороннее HTTP). Никаких Python imports между ролями. Каждая роль имеет **свой** Postgres и Redis.

---

## Roadmap по Phases

### Phase 1 — Foundation (Wave A) — done

- A.1: первый коммит `Tmwyw/orchestrator` baseline
- A.2: организация папок (`netrun-prod/{orchestrator,node_runtime,tg_bot}`)

### Phase 2 — Backend (Wave B) — почти завершена

| Wave | Status | Содержание |
|---|---|---|
| B-0 | closed | toolchain (ruff/mypy/pytest), pyproject.toml, async refactor основания |
| B-1 | closed | 10 SQL миграций (skus, sku_node_bindings, proxy_inventory, orders, delivery_files, etc.) + 8 Pydantic v2 моделей |
| B-2 | closed | refill engine: `RefillService`, `GenerationWorker`, distribution (equal_share) |
| B-3 | closed | validation pipeline: `ProxyValidationService` (SOCKS5/HTTP probe + geo + ipv6) |
| B-4 | closed | sale-domain: orders/reserve/commit/release/extend, delivery (4 формата), allocator |
| B-5 | closed | integration smoke на 95.217.98.125 + 3 нодах. Real purchase `ord_0946ecd02fcb`. **Hotfix B-5b** (FOR UPDATE с aggregate → advisory_xact_lock) |
| B-6 | closed | node enrollment: `GET /describe`, `POST /v1/nodes/enroll`, `enroll-node.sh` CLI. **Hotfix B-6.3** (ON CONFLICT id → url) |
| B-7a | closed | production hardening: systemd units для refill+validation+watchdog, `WatchdogService`, /v1/* aliases, scripts → /v1 |
| B-7b.1 | closed | structured JSON logging (structlog stdlib bridge + per-file event-style rename) |
| B-7b.2 | closed | Prometheus metrics + /metrics endpoint + HTTP middleware + instrumentation |
| B-7b.3 | closed | admin endpoints `/v1/admin/{stats,orders,archive}` + Pydantic response models + Decimal serialization convention |
| B-7b.4 | closed | validation hardening: VALIDATION_STRICT_SSL flag (#5) + log_job_event в 3 except-ветках process_refill_job (#11) |
| B-7b.5 | closed | network hardening (#17): bind 127.0.0.1 default + opt-in scripts/install_nginx.sh |
| B-7c | closed | мета: `docs/prompt_style.md` гайд для будущих Wave-промптов |
| B-7d | closed (this Wave) | housekeeping: roadmap + backfill known-issues |
| B-8 | pending | Pay-per-GB billing: per-user dedicated port, nftables polling, `traffic_accounts` table, тарифные tiers. Cross-repo (orchestrator + node_runtime) |

После B-8: orchestrator готов **самостоятельно** обслуживать продажи без bot'а.

### Phase 3 — Bot integration (Wave C) — pending

Подразделится на sub-wave'ы C.1..C.N. Примерный scope:

| Sub-wave | Что |
|---|---|
| C.1 | Создать `Tmwyw/netrun_bot` baseline + CI + ruff/mypy/pytest |
| C.2 | Решить: переносить tg_bot из `NETRUN FINAL/tg_bot/` (быстро, тащит mojibake) или писать с нуля (чище). По текущему плану — **перенести**, потом постепенно cleanup |
| C.3 | `tg_bot/services/orchestrator_client.py` — HTTP-клиент с типами (Pydantic v2 модели из orchestrator's openapi) |
| C.4 | Замена прямых импортов `from orchestrator.app.proxy_domain import ...` (legacy в `NETRUN FINAL/tg_bot/proxy_user/checkout.py`) → HTTP вызовы к `/v1/orders/reserve` |
| C.5 | Atomic balance (PR #1.5 steps A+B из NETRUN FINAL — портировать) |
| C.6 | Decimal migration для money fields (PR #1.6 из старого плана) |
| C.7 | Notifications за 3/2/1 дня до expire (bot polling `GET /v1/orders/expiring`, нужно реализовать endpoint в orchestrator) |
| C.8 | Pay-per-GB UX в bot (после B-8) — выбор тарифа, отображение оставшегося трафика |
| C.9 | Удалить legacy code (`proxy_user/checkout.py`, прямые imports) |
| C.10 | CI/CD на private repo (если в Wave D репо станет private) |

**Интеграционный smoke C-final**: реальная покупка через bot UI → orchestrator API → нода → пользователь получает прокси в чат.

### Phase 4 — Production launch (Wave D) — pending

| Sub-wave | Что |
|---|---|
| D.1 | TLS via certbot (для nginx из B-7b.5) — manual operator step, документировано в operations.md § 11 |
| D.2 | Backup strategy: `pg_dump` + Redis SAVE по cron, retention 30/90 дней, off-site копия |
| D.3 | Domain + DNS (для bot webhook, для orchestrator API через nginx server_name) |
| D.4 | Платёжная интеграция: крипто (USDT TRC-20, BTC?) + крипто-обменник внутри бота. Маржа на конвертации |
| D.5 | TOS, Privacy Policy, AML/KYC (если потребуется на крипто-обмен) |
| D.6 | Monitoring + alerting: Prometheus → Grafana ИЛИ простые Telegram-алерты в админ-чат от orchestrator |
| D.7 | Maintenance tooling: rolling restart, deploy script с rollback, db migration runbook |
| D.8 | Soft launch (бета-тестеры) → public launch |

### Phase 5 — Scale (Wave E+, по мере роста) — далёкий горизонт

Не нужны до тех пор пока не упёрлись:

| Когда | Что |
|---|---|
| 30+ нод | enroll-node CLI критичен (`scripts/enroll-node.sh` уже есть с B-6) |
| 100+ нод | автоматизация: discovery service, automated node provisioning через Terraform/Ansible |
| 300+ нод | sharding: либо несколько orchestrator-инстансов (по гео?), либо read replica Postgres'а |
| 10k RPS | pgbouncer transaction-mode + connection pool tuning, async refactor remaining sync code |
| Bot 50k+ users | bot horizontal scaling: long-polling → webhook + nginx, или Telethon + load balancer |
| 100k+ orders/day | data archival: старые orders → отдельный warehouse (S3 + Parquet?), keep hot DB lean |

---

## Production Infrastructure (текущее состояние)

```
1 orchestrator-сервер (95.217.98.125, Ubuntu 24.04)
  - Postgres 16 (netrun_orchestrator DB)
  - Redis 7
  - 5 systemd units (после B-7a):
      netrun-orchestrator (FastAPI :8090, после B-7b.5 default 127.0.0.1)
      netrun-orchestrator-worker (generation jobs)
      netrun-orchestrator-refill (scheduler)
      netrun-orchestrator-validation (scheduler)
      netrun-orchestrator-watchdog (scheduler, после B-7a)

3 ноды (Vultr, ipv6_only):
  - 139.84.219.149 (node-1)
  - 65.20.80.21 (node-2)
  - 65.20.72.62 (node-3)
  - Каждая: 4GB RAM, 2 CPU, /opt/netrun, node-agent :8085, 3proxy + nftables

1 SKU active: ipv6_us_socks5 (target_stock=30, 3 active bindings)

[Будущий] Bot-сервер (TBD, в Wave D):
  - Postgres (bot DB — отдельный от orchestrator)
  - Redis (bot cache, rate limit, idempotency)
  - python -m tg_bot.app.bot (user-bot)
  - python -m tg_bot.app.admin_bot (admin-bot)
```

---

## Out-of-scope (намеренно НЕ делаем)

| Что | Почему |
|---|---|
| Microservices декомпозиция orchestrator (на N сервисов) | Premature optimization; monolith с 5 systemd units справляется до 10k RPS |
| Multi-region active-active deployment | Наша целевая нагрузка не требует; добавляет операционную сложность |
| Multi-tenancy (один deployment = один клиент) | Мы B2C, не B2B platform |
| Kubernetes / Docker Swarm orchestration | Bare-metal systemd достаточно. K8s приходит когда есть >10 серверов с одной ролью |
| Custom payment processor | Используем существующие крипто-сервисы (CryptoCloud, Coinpayments или прямые integrations) |
| ML / fraud detection | Add-on в будущем, не blocker запуска |

---

## Известные риски и mitigations

| Риск | Как смягчаем |
|---|---|
| Vultr ipv6 instability (флаппинг ipv6.ok=false) | Watchdog в B-7a авто-recover'ит ноды; --force флаг в enroll; план B — мульти-провайдер |
| Pool overshoot при медленной генерации (issue #1) | Watchdog в B-7a корректирует, refill respects refill_batch_size cap |
| Bruteforce на ORCHESTRATOR_API_KEY (порт 8090 публичный) | B-7b.5 — bind 127.0.0.1 default + opt-in nginx с TLS (Wave D.1) |
| Money in float (legacy) | Decimal migration в Wave C-6; convention зафиксирована в design.md § 6.10 (Wave B-7b.3) |
| Orchestrator vs bot версия рассинхронизируется | OpenAPI yaml автогенерится из FastAPI, bot использует как контракт |
| Юзер закроет бота между reserve и commit | Reservation TTL в Redis (default 300s), watchdog освобождает inventory обратно |

---

## Точки коммуникации с пользователем

| Когда | Что |
|---|---|
| Каждая Wave end | Diff-review + «ок пуш» gate |
| Production deploy after push | Manual ssh + git pull + restart (`operations.md` есть playbook) |
| Перед потреблением context window | Starter-промпт для нового чата (этот документ — часть его) |
| Переход к Wave C | Открыть Claude Code в `netrun-prod\netrun-tg_bot\`, новый стартер с фокусом на bot |

---

## Где найти что (мета)

| Документ | Где | Зачем |
|---|---|---|
| `docs/wave_b_design.md` | `Tmwyw/orchestrator` | Архитектурное «конституция» — schema, decisions log, 18 known issues |
| `docs/operations.md` | `Tmwyw/orchestrator` | Operations playbook (install, enroll, smoke, monitor, troubleshoot, nginx) |
| `docs/prompt_style.md` | `Tmwyw/orchestrator` | Гайд по написанию Wave-промптов |
| `docs/roadmap.md` | `Tmwyw/orchestrator` (этот файл) | Project-wide context |
| `README.md` (per repo) | каждый репо | Quick-start для каждой роли |

---

## Текущий план движения

```
TODAY → B-7d (closed) → B-8 design-pass (отдельная сессия)
     → B-8 execution (cross-repo, 2-3 недели)
─────── Phase 2 backend done ───────
     → Wave C (bot integration) — отдельный чат, отдельный Claude Code в netrun-tg_bot
─────── Phase 3 bot done ───────
     → Wave D (production launch) — soft → hard launch
─────── Live system, продажи идут ───────
     → Wave E+ scale optimizations по мере роста
```

---

**Это контекст, не to-do.** Конкретные Wave-промпты будут писаться когда дойдём.
