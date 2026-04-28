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
