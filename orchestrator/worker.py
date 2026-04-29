import logging
import time
from typing import Any

import httpx

from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.jobs import (
    allocate_start_port,
    bulk_insert_inventory_pending,
    log_job_event,
    normalize_proxy_items,
    response_diagnostics,
    select_node,
    write_proxies_file,
)
from orchestrator.logging_setup import configure_logging
from orchestrator.node_client import generate
from shared.contracts import PRODUCTION_PROFILE

configure_logging()
logger = logging.getLogger("netrun-orchestrator-worker")


def claim_next_job() -> dict[str, Any] | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select *
                from jobs
                where status = 'queued'
                order by created_at asc
                for update skip locked
                limit 1
                """
            )
            job = cur.fetchone()
            if not job:
                return None
            cur.execute(
                """
                update jobs
                set status = 'running', updated_at = now()
                where id = %s
                returning *
                """,
                (job["id"],),
            )
            claimed = cur.fetchone()
        log_job_event(
            conn, claimed["id"], "running", {"profile": PRODUCTION_PROFILE, "ipv6_policy": "ipv6_only"}
        )
        return dict(claimed)


def mark_failed(job_id: str, error: str, event_data: dict[str, Any]) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update jobs
                set status = 'failed', error = %s, updated_at = now()
                where id = %s
                """,
                (error, job_id),
            )
        log_job_event(conn, job_id, "failed", {"result": error, **event_data})


def mark_success(job_id: str, result_path: str, event_data: dict[str, Any]) -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                update jobs
                set status = 'success', result_path = %s, error = null, updated_at = now()
                where id = %s
                """,
                (result_path, job_id),
            )
        log_job_event(conn, job_id, "success", {"result": result_path, **event_data})


def assign_node_and_port(job: dict[str, Any]) -> tuple[dict[str, Any], int]:
    node = select_node(int(job["count"]))
    with connect() as conn:
        start_port = allocate_start_port(conn, node["id"], int(job["count"]))
        with conn.cursor() as cur:
            cur.execute(
                """
                update jobs
                set node_id = %s, start_port = %s, updated_at = now()
                where id = %s
                """,
                (node["id"], start_port, job["id"]),
            )
        log_job_event(conn, job["id"], "assigned", {"node": node["id"], "start_port": start_port})
    return node, start_port


def process_job(job: dict[str, Any]) -> None:
    sku_id = job.get("sku_id")
    reason = job.get("reason") or "manual"
    if sku_id is not None and reason == "refill":
        process_refill_job(job)
    else:
        process_simple_job(job)


def process_simple_job(job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    try:
        node, start_port = assign_node_and_port(job)
        logger.info(
            "job generation start job_id=%s node=%s profile=%s ipv6_policy=ipv6_only",
            job_id,
            node["id"],
            PRODUCTION_PROFILE["fingerprint_profile_version"],
        )
        result = generate(
            url=node["url"],
            api_key=node.get("api_key"),
            job_id=job_id,
            count=int(job["count"]),
            start_port=start_port,
            timeout_sec=get_config().node_request_timeout_sec,
        )

        event_base = {
            "node": node["id"],
            "profile": PRODUCTION_PROFILE,
            "ipv6_policy": "ipv6_only",
            "node_response": response_diagnostics(result),
        }
        if result.get("success") is not True or result.get("status") != "ready":
            logger.warning("generation_failed job_id=%s diagnostics=%s", job_id, response_diagnostics(result))
            mark_failed(job_id, "generation_failed", event_base)
            return

        items = result.get("items")
        if not isinstance(items, list) or len(items) < int(job["count"]):
            logger.warning(
                "node_response_missing_items job_id=%s diagnostics=%s", job_id, response_diagnostics(result)
            )
            mark_failed(job_id, "node_response_missing_items", event_base)
            return

        proxy_lines = normalize_proxy_items(items[: int(job["count"])])
        result_path = write_proxies_file(job_id, proxy_lines)
        mark_success(job_id, str(result_path), event_base)
        logger.info("job success job_id=%s node=%s result=%s", job_id, node["id"], result_path)

    except httpx.RequestError as exc:
        mark_failed(
            job_id,
            "node_unavailable",
            {"profile": PRODUCTION_PROFILE, "ipv6_policy": "ipv6_only", "detail": str(exc)},
        )
        logger.warning("job failed job_id=%s error=node_unavailable detail=%s", job_id, exc)
    except RuntimeError as exc:
        error = (
            str(exc)
            if str(exc) in {"capacity_not_available", "generation_failed", "node_unavailable"}
            else "generation_failed"
        )
        mark_failed(
            job_id, error, {"profile": PRODUCTION_PROFILE, "ipv6_policy": "ipv6_only", "detail": str(exc)}
        )
        logger.warning("job failed job_id=%s error=%s", job_id, error)
    except Exception as exc:
        mark_failed(
            job_id,
            "generation_failed",
            {"profile": PRODUCTION_PROFILE, "ipv6_policy": "ipv6_only", "detail": str(exc)},
        )
        logger.exception("job failed job_id=%s error=generation_failed", job_id)


def process_refill_job(job: dict[str, Any]) -> None:
    """Process a refill job: bulk-insert generated proxies into ``proxy_inventory``.

    Refill jobs (reason='refill', sku_id NOT NULL) are pre-assigned a node and
    a port range at enqueue time by :class:`RefillService`. The worker only
    invokes the node's ``/generate`` endpoint and imports the result into
    ``proxy_inventory`` with status='pending_validation' for the validation
    worker to process. No proxies file is written for refill jobs.
    """
    job_id = str(job["id"])
    sku_id_raw = job.get("sku_id")
    node_id = job.get("node_id")
    start_port = job.get("start_port")
    count = int(job["count"])

    if sku_id_raw is None or not node_id or start_port is None:
        mark_failed(job_id, "refill_job_missing_assignment", {"node_id": node_id, "start_port": start_port})
        return

    sku_id = int(sku_id_raw)
    node_id = str(node_id)

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("select * from nodes where id = %s", (node_id,))
            node_row = cur.fetchone()
        if not node_row:
            mark_failed(job_id, "node_not_found", {"node_id": node_id})
            return
        node = dict(node_row)

        logger.info(
            "refill job generation start job_id=%s node=%s sku_id=%s count=%s start_port=%s",
            job_id,
            node_id,
            sku_id,
            count,
            start_port,
        )
        result = generate(
            url=node["url"],
            api_key=node.get("api_key"),
            job_id=job_id,
            count=count,
            start_port=int(start_port),
            timeout_sec=get_config().node_request_timeout_sec,
        )

        event_base = {
            "node": node_id,
            "sku_id": sku_id,
            "profile": PRODUCTION_PROFILE,
            "ipv6_policy": "ipv6_only",
            "node_response": response_diagnostics(result),
        }
        if result.get("success") is not True or result.get("status") != "ready":
            logger.warning(
                "refill generation_failed job_id=%s diagnostics=%s",
                job_id,
                response_diagnostics(result),
            )
            mark_failed(job_id, "generation_failed", event_base)
            return

        items = result.get("items")
        if not isinstance(items, list) or len(items) < count:
            logger.warning(
                "refill node_response_missing_items job_id=%s diagnostics=%s",
                job_id,
                response_diagnostics(result),
            )
            mark_failed(job_id, "node_response_missing_items", event_base)
            return

        inserted = bulk_insert_inventory_pending(
            sku_id=sku_id,
            node_id=node_id,
            generation_job_id=job_id,
            items=items[:count],
        )
        if inserted == 0:
            mark_failed(job_id, "inventory_insert_zero", event_base)
            return

        mark_success(
            job_id,
            "",
            {**event_base, "imported_to_pending_validation": inserted},
        )
        logger.info(
            "refill job success job_id=%s sku=%s node=%s inserted=%s",
            job_id,
            sku_id,
            node_id,
            inserted,
        )

    except httpx.RequestError as exc:
        mark_failed(
            job_id,
            "node_unavailable",
            {
                "profile": PRODUCTION_PROFILE,
                "ipv6_policy": "ipv6_only",
                "detail": str(exc),
                "node": node_id,
                "sku_id": sku_id,
            },
        )
        logger.warning("refill job failed job_id=%s error=node_unavailable detail=%s", job_id, exc)
    except RuntimeError as exc:
        error = (
            str(exc)
            if str(exc) in {"capacity_not_available", "generation_failed", "node_unavailable"}
            else "generation_failed"
        )
        mark_failed(
            job_id,
            error,
            {
                "profile": PRODUCTION_PROFILE,
                "ipv6_policy": "ipv6_only",
                "detail": str(exc),
                "node": node_id,
                "sku_id": sku_id,
            },
        )
        logger.warning("refill job failed job_id=%s error=%s", job_id, error)
    except Exception as exc:
        mark_failed(
            job_id,
            "internal_error",
            {
                "profile": PRODUCTION_PROFILE,
                "ipv6_policy": "ipv6_only",
                "detail": str(exc),
                "node": node_id,
                "sku_id": sku_id,
            },
        )
        logger.exception("refill job failed job_id=%s error=internal_error", job_id)


def run_once() -> bool:
    job = claim_next_job()
    if not job:
        return False
    process_job(job)
    return True


def run_loop() -> None:
    poll_sec = max(1, get_config().worker_poll_interval_sec)
    logger.info("worker loop started poll_sec=%s", poll_sec)
    while True:
        processed = run_once()
        if not processed:
            time.sleep(poll_sec)


def main() -> None:
    run_loop()


if __name__ == "__main__":
    main()
