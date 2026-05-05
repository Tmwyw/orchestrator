"""Prometheus metrics for orchestrator.

All metrics use a unified ``status`` label with values ``"success"`` or
``"failed"`` only — never ``"ok"``, ``"error"``, ``"fail"``. HTTP path
label uses FastAPI route template (e.g. ``/v1/orders/{order_ref}/commit``),
not concrete URL with values, to avoid cardinality explosion.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# === Allocator ===

RESERVE_TOTAL = Counter(
    "netrun_reserve_total",
    "Reserve calls",
    ["status", "error"],
)
RESERVE_DURATION_SEC = Histogram(
    "netrun_reserve_duration_sec",
    "POST /v1/orders/reserve duration",
)
COMMIT_TOTAL = Counter(
    "netrun_commit_total",
    "Commit calls",
    ["status"],
)
RELEASE_TOTAL = Counter(
    "netrun_release_total",
    "Release calls",
    ["status"],
)

# === Schedulers (refill, validation, watchdog) ===

SCHEDULER_RUN_TOTAL = Counter(
    "netrun_scheduler_run_total",
    "Scheduler runs",
    ["scheduler", "status"],
)
SCHEDULER_RUN_DURATION_SEC = Histogram(
    "netrun_scheduler_run_duration_sec",
    "Scheduler run_once() duration",
    ["scheduler"],
)

# === Watchdog actions (per-counter from WatchdogService.run_once()) ===

WATCHDOG_ACTIONS = Counter(
    "netrun_watchdog_actions_total",
    "Watchdog recovery actions",
    ["action"],
)

# === HTTP middleware (every request) ===

HTTP_REQUESTS = Counter(
    "netrun_http_requests_total",
    "HTTP requests",
    ["method", "path", "status"],
)
HTTP_DURATION_SEC = Histogram(
    "netrun_http_duration_sec",
    "HTTP request duration",
    ["method", "path"],
)

# === Inventory pool snapshot (refreshed by validation/refill schedulers,
# wiring via SET in B-7b.2 is optional; skipping if costly). ===

INVENTORY_AVAILABLE = Gauge(
    "netrun_inventory_available",
    "Available inventory rows",
    ["sku_code", "node_id"],
)

# === Pay-per-GB traffic poll (Wave B-8.2) ===

TRAFFIC_POLL_TOTAL = Counter(
    "netrun_traffic_poll_total",
    "Per-node traffic poll attempts",
    ["node_id", "status"],
)
TRAFFIC_POLL_DURATION_SEC = Histogram(
    "netrun_traffic_poll_duration_sec",
    "Per-node /accounting GET duration",
    ["node_id"],
)
TRAFFIC_ACCOUNTS_ACTIVE = Gauge(
    "netrun_traffic_accounts_active",
    "traffic_accounts rows in status='active'",
)
TRAFFIC_ACCOUNTS_DEPLETED = Gauge(
    "netrun_traffic_accounts_depleted",
    "traffic_accounts rows in status='depleted'",
)
TRAFFIC_COUNTER_RESET_TOTAL = Counter(
    "netrun_traffic_counter_reset_total",
    "Counter-reset events detected (delta < 0)",
    ["node_id"],
)
TRAFFIC_POLL_LAG_SEC = Gauge(
    "netrun_traffic_poll_lag_sec",
    "now() - oldest active.last_polled_at (seconds)",
)
TRAFFIC_OVER_USAGE_TOTAL = Counter(
    "netrun_traffic_over_usage_total",
    "Cycles where bytes_used > bytes_quota at depletion",
)
TRAFFIC_BYTES_TOTAL = Counter(
    "netrun_traffic_bytes_total",
    "Cumulative billed bytes by SKU + direction",
    ["sku_code", "direction"],
)
