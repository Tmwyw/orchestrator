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
