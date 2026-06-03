import logging
import re
from pathlib import Path
from typing import Any

from orchestrator.config import get_config
from orchestrator.db import connect, fetch_all, fetch_one
from orchestrator.node_client import check_health
from shared.contracts import PRODUCTION_PROFILE

logger = logging.getLogger("netrun-orchestrator")
PROXY_LINE_RE = re.compile(r"^[^:]+:[0-9]{1,5}:[^:]+:[^:]+$")


def public_job(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "status": row["status"],
        "count": row["count"],
        "product": row["product"],
        "created_at": row["created_at"],
        "result_path": row.get("result_path"),
        "node_id": row.get("node_id"),
        "start_port": row.get("start_port"),
        "idempotency_key": row.get("idempotency_key"),
        "error": row.get("error"),
        "profile": row.get("profile") or PRODUCTION_PROFILE,
    }


def response_diagnostics(response: dict[str, Any]) -> dict[str, Any]:
    items = response.get("items")
    return {
        "success": response.get("success"),
        "status": response.get("status"),
        "error": response.get("error"),
        "generatedCount": response.get("generatedCount"),
        "expectedCount": response.get("expectedCount"),
        "itemsType": type(items).__name__,
        "itemsCount": len(items) if isinstance(items, list) else None,
        "jobId": response.get("jobId"),
        "jobDir": response.get("jobDir"),
        "output": response.get("output"),
        "logs": response.get("logs"),
        "profile": response.get("profile"),
        "diagnostics": response.get("diagnostics"),
    }


def node_health_diagnostics(health: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": health.get("success"),
        "status": health.get("status"),
        "ipv6": health.get("ipv6"),
        "ipv6Egress": health.get("ipv6Egress"),
        "error": health.get("error"),
    }


def node_health_ipv6_ok(health: dict[str, Any]) -> bool:
    ipv6 = health.get("ipv6")
    if isinstance(ipv6, dict) and ipv6.get("ok") is True:
        return True

    ipv6_egress = health.get("ipv6Egress")
    return isinstance(ipv6_egress, dict) and ipv6_egress.get("ok") is True


def node_health_ready(health: dict[str, Any]) -> bool:
    return health.get("success") is True and health.get("status") == "ready" and node_health_ipv6_ok(health)


def log_job_event(conn, job_id: str, event: str, data: dict[str, Any]) -> None:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            "insert into job_events(job_id, event, data) values (%s, %s, %s)",
            (job_id, event, Jsonb(data)),
        )


def update_node_health(conn, node: dict[str, Any], status: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            update nodes
            set status = %s, last_health_check = now(), updated_at = now()
            where id = %s
            """,
            (status, node["id"]),
        )


def select_node(count: int) -> dict[str, Any]:
    rows = fetch_all(
        """
        select * from nodes
        where capacity >= %s
        order by
          case when status = 'ready' then 0 else 1 end,
          capacity desc,
          created_at asc
        """,
        (count,),
    )
    if not rows:
        all_count = fetch_one("select count(*) as c from nodes")
        if all_count and int(all_count["c"]) > 0:
            raise RuntimeError("capacity_not_available")
        raise RuntimeError("node_unavailable")

    for node in rows:
        try:
            health = check_health(node["url"], node.get("api_key"), timeout_sec=10)
            ready = node_health_ready(health)
        except Exception as exc:
            logger.warning("node health failed node=%s error=%s", node["id"], exc)
            with connect() as conn:
                update_node_health(conn, node, "unavailable")
            continue
        with connect() as conn:
            update_node_health(conn, node, "ready" if ready else "unavailable")
        if ready:
            node["status"] = "ready"
            return node

    raise RuntimeError("node_unavailable")


def allocate_start_port(conn, node_id: str, count: int) -> int:
    cfg = get_config()
    with conn.cursor() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", (f"node_ports:{node_id}",))
        cur.execute(
            """
            select coalesce(max(start_port + count), %s) as next_port
            from jobs
            where node_id = %s and start_port is not null
            """,
            (cfg.start_port_min, node_id),
        )
        row = cur.fetchone()
    start_port = int(row["next_port"] or cfg.start_port_min)
    if start_port < cfg.start_port_min:
        start_port = cfg.start_port_min
    if start_port + count > cfg.start_port_max:
        raise RuntimeError("capacity_not_available")
    return start_port


def allocate_port_range_via_table(conn, *, node_id: str, job_id: str, count: int) -> tuple[int, int]:
    """Reserve a port range via ``node_port_allocations`` and return (start, end).

    Concurrent callers on the same node are serialized by a transaction-scoped
    advisory lock (``pg_advisory_xact_lock`` keyed on ``node_ports:{node_id}``);
    the lock auto-releases on commit/rollback. ``FOR UPDATE`` cannot be combined
    with the ``MAX(...)`` aggregate, so we use the same advisory-lock pattern as
    :func:`allocate_start_port`. The caller MUST already have an INSERT'd row in
    ``jobs`` for ``job_id`` (FK constraint requires it). The returned range is
    also written into ``node_port_allocations`` with status='reserved' inside
    the same transaction.
    """
    if count <= 0:
        raise ValueError("count must be positive")
    cfg = get_config()
    with conn.cursor() as cur:
        cur.execute(
            "select pg_advisory_xact_lock(hashtext(%s))",
            (f"node_ports:{node_id}",),
        )
        cur.execute(
            """
            select coalesce(max(end_port), 0) as max_end
            from node_port_allocations
            where node_id = %s and status = 'reserved'
            """,
            (node_id,),
        )
        row = cur.fetchone()
        max_end = int((row or {}).get("max_end") or 0)
        start_port = max(cfg.start_port_min, max_end + 1)
        end_port = start_port + count - 1
        if end_port > cfg.start_port_max:
            raise RuntimeError("capacity_not_available")
        cur.execute(
            """
            insert into node_port_allocations
              (job_id, node_id, start_port, end_port, proxy_count, status)
            values (%s, %s, %s, %s, %s, 'reserved')
            """,
            (job_id, node_id, start_port, end_port, count),
        )
    return start_port, end_port


def normalize_proxy_items(items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in items:
        host = str(item.get("host") or "").strip()
        port = int(item.get("port") or 0)
        login = str(item.get("login") or "").strip()
        password = str(item.get("password") or "").strip()
        line = f"{host}:{port}:{login}:{password}"
        if not PROXY_LINE_RE.match(line):
            raise RuntimeError("generation_failed")
        lines.append(line)
    return lines


def write_proxies_file(job_id: str, lines: list[str]) -> Path:
    cfg = get_config()
    job_dir = cfg.jobs_root / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    result_path = job_dir / "proxies.list"
    result_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result_path


def collapse_dual_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wave HTTP.B — fold a dual node-agent report (TWO URIs per IP — a
    socks5 line + a paired http line at ``socks_port - 10000``) into ONE
    logical proxy per IP, so the pool does NOT double.

    The node-agent (HTTP.A) tags each reported item with ``protocol``
    ('socks5' | 'http'). We keep the socks5 (or legacy / untagged) items
    as the canonical inventory rows and attach the paired http port to
    each as ``http_port`` (matched on same ``host`` + ``port - 10000``).
    The http items are consumed, never emitted as their own rows.

    Backward-compat: a socks5-only report (old node-agent / single mode)
    has no http items → every logical row gets ``http_port = None`` and
    the result is identical to the raw item list.
    """
    http_ports: set[tuple[str, int]] = set()
    socks_items: list[dict[str, Any]] = []
    for item in items:
        protocol = str(item.get("protocol") or "socks5").strip().lower()
        host = str(item.get("host") or "").strip()
        port_raw = item.get("port")
        try:
            port = int(port_raw) if port_raw is not None else 0
        except (TypeError, ValueError):
            port = 0
        if port <= 0:
            # Keep malformed socks items so the downstream validator skips
            # them consistently; http items with bad ports are just dropped.
            if protocol != "http":
                socks_items.append(item)
            continue
        if protocol == "http":
            http_ports.add((host, port))
        else:
            socks_items.append(item)

    logical: list[dict[str, Any]] = []
    for item in socks_items:
        host = str(item.get("host") or "").strip()
        port_raw = item.get("port")
        try:
            port = int(port_raw) if port_raw is not None else 0
        except (TypeError, ValueError):
            port = 0
        if port <= 0:
            logical.append({**item, "http_port": None})
            continue
        paired = (host, port - 10000)
        http_port = port - 10000 if paired in http_ports else None
        logical.append({**item, "http_port": http_port})
    return logical


def bulk_insert_inventory_pending(
    *,
    sku_id: int,
    node_id: str,
    generation_job_id: str,
    items: list[dict[str, Any]],
) -> int:
    """Insert generated proxies into ``proxy_inventory`` with status='pending_validation'.

    Each item must have: ``host``, ``port``, ``login``, ``password``, and
    optionally ``http_port`` (Wave HTTP.B — the paired http listener port
    for dual proxies; ``None``/absent = socks5-only). Items missing any
    required field are skipped. Duplicate ``(login, password, host, port)``
    tuples within the same batch are also skipped. Returns the number of
    rows actually inserted.

    The caller is expected to pass dual reports through
    :func:`collapse_dual_items` first so one IP is one row (the http line
    rides on ``http_port``, not a second row → the pool never doubles).
    """
    seen: set[tuple[str, str, str, int]] = set()
    rows: list[tuple[int, str, str, str, str, str, int, int | None]] = []
    for item in items:
        host = str(item.get("host") or "").strip()
        port_raw = item.get("port")
        login = str(item.get("login") or "").strip()
        password = str(item.get("password") or "").strip()
        if not host or not login or not password or port_raw is None:
            continue
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            continue
        if port <= 0 or port > 65535:
            continue
        http_port_raw = item.get("http_port")
        http_port: int | None = None
        if http_port_raw is not None:
            try:
                hp = int(http_port_raw)
            except (TypeError, ValueError):
                hp = 0
            if 0 < hp <= 65535:
                http_port = hp
        key = (login, password, host, port)
        if key in seen:
            continue
        seen.add(key)
        rows.append((sku_id, node_id, generation_job_id, login, password, host, port, http_port))

    if not rows:
        return 0

    with connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            insert into proxy_inventory
              (sku_id, node_id, generation_job_id, login, password, host, port, http_port, status)
            values (%s, %s, %s, %s, %s, %s, %s, %s, 'pending_validation')
            """,
            rows,
        )
    return len(rows)
