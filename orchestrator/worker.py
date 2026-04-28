import logging
import time
from typing import Any

import httpx

from orchestrator.config import get_config
from orchestrator.db import connect
from orchestrator.jobs import (
    allocate_start_port,
    log_job_event,
    normalize_proxy_items,
    response_diagnostics,
    select_node,
    write_proxies_file,
)
from orchestrator.node_client import generate
from shared.contracts import PRODUCTION_PROFILE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
