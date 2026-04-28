import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from psycopg.types.json import Jsonb

from orchestrator.config import get_config
from orchestrator.db import connect, fetch_all, fetch_one
from orchestrator.jobs import log_job_event, node_health_diagnostics, node_health_ready, public_job
from orchestrator.node_client import check_health
from shared.contracts import FORBIDDEN_JOB_FIELDS, PRODUCTION_PROFILE


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("netrun-orchestrator")

app = FastAPI(
    title="NETRUN Orchestrator",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

ALLOWED_PRODUCTS = {"android_ipv6_only", "smoke"}
ALLOWED_JOB_FIELDS = {"count", "product", "idempotency_key"}
ALLOWED_NODE_FIELDS = {"id", "name", "url", "geo", "capacity", "api_key", "force"}


def require_api_key(x_netrun_api_key: str | None = Header(default=None)) -> None:
    cfg = get_config()
    if not cfg.api_key:
        raise HTTPException(status_code=500, detail="orchestrator_api_key_not_configured")
    if x_netrun_api_key != cfg.api_key:
        raise HTTPException(status_code=401, detail="unauthorized")


def error_response(status_code: int, error: str, **extra: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "status": "failed", "error": error, **extra},
    )


def public_node(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "url": row["url"],
        "geo": row.get("geo") or "",
        "status": row.get("status") or "unknown",
        "capacity": row.get("capacity") or 0,
        "last_health_check": row.get("last_health_check"),
    }


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def validate_node_payload(payload: dict[str, Any]) -> dict[str, Any]:
    extra = set(payload) - ALLOWED_NODE_FIELDS
    if extra:
        raise ValueError(f"unsupported_node_fields:{','.join(sorted(extra))}")

    node_url = str(payload.get("url") or "").strip().rstrip("/")
    if not node_url.startswith(("http://", "https://")):
        raise ValueError("node_url_must_be_http")

    capacity = int(payload.get("capacity") or 0)
    if capacity <= 0:
        raise ValueError("node_capacity_must_be_positive")

    node_id = str(payload.get("id") or uuid.uuid4()).strip()
    name = str(payload.get("name") or node_id).strip()
    return {
        "id": node_id,
        "name": name,
        "url": node_url,
        "geo": str(payload.get("geo") or "").strip(),
        "capacity": capacity,
        "api_key": str(payload.get("api_key") or "").strip() or None,
        "force": parse_bool(payload.get("force")),
    }


def validate_job_payload(payload: dict[str, Any]) -> dict[str, Any]:
    forbidden = sorted(set(payload) & FORBIDDEN_JOB_FIELDS)
    extra = sorted(set(payload) - ALLOWED_JOB_FIELDS)
    if forbidden or extra:
        raise ValueError(
            json.dumps(
                {
                    "error": "invalid_product_contract",
                    "forbidden_fields": forbidden,
                    "unsupported_fields": extra,
                },
                separators=(",", ":"),
            )
        )

    count = int(payload.get("count") or 0)
    product = str(payload.get("product") or "").strip()
    idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
    if count <= 0:
        raise ValueError("job_count_must_be_positive")
    if product not in ALLOWED_PRODUCTS:
        raise ValueError("invalid_product")
    if idempotency_key and len(idempotency_key) > 128:
        raise ValueError("idempotency_key_too_long")
    return {"count": count, "product": product, "idempotency_key": idempotency_key}


@app.get("/health", dependencies=[Depends(require_api_key)])
def health():
    try:
        fetch_one("select 1 as ok")
    except Exception as exc:
        return error_response(500, "database_unavailable", detail=str(exc))
    return {"success": True, "status": "ready", "service": "netrun-orchestrator"}


@app.get("/nodes", dependencies=[Depends(require_api_key)])
def list_nodes():
    rows = fetch_all("select * from nodes order by created_at asc")
    return {"success": True, "status": "ready", "items": [public_node(row) for row in rows]}


@app.post("/nodes", dependencies=[Depends(require_api_key)])
async def create_node(request: Request):
    payload = await request.json()
    try:
        node = validate_node_payload(payload)
    except Exception as exc:
        return error_response(400, str(exc))

    node_status = "ready"
    try:
        node_health = check_health(node["url"], node["api_key"], timeout_sec=10)
    except Exception as exc:
        if not node["force"]:
            return error_response(409, "node_unavailable", detail=str(exc))
        node_status = "unavailable"
        node_health = {"error": str(exc)}
    else:
        if not node_health_ready(node_health):
            if not node["force"]:
                return error_response(409, "node_unavailable", node_health=node_health_diagnostics(node_health))
            node_status = "unavailable"

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into nodes(id, name, url, geo, capacity, api_key, status, last_health_check)
                values (%s, %s, %s, %s, %s, %s, %s, now())
                on conflict (id) do update set
                  name = excluded.name,
                  url = excluded.url,
                  geo = excluded.geo,
                  capacity = excluded.capacity,
                  api_key = excluded.api_key,
                  status = excluded.status,
                  last_health_check = now(),
                  updated_at = now()
                returning *
                """,
                (
                    node["id"],
                    node["name"],
                    node["url"],
                    node["geo"],
                    node["capacity"],
                    node["api_key"],
                    node_status,
                ),
            )
            row = cur.fetchone()
    return {"success": True, "status": "ready", "item": public_node(row)}


@app.delete("/nodes/{node_id}", dependencies=[Depends(require_api_key)])
def delete_node(node_id: str):
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("delete from nodes where id = %s returning id", (node_id,))
            row = cur.fetchone()
    if not row:
        return error_response(404, "node_not_found")
    return {"success": True, "status": "ready", "deleted": node_id}


@app.post("/jobs", dependencies=[Depends(require_api_key)])
async def create_job(request: Request):
    payload = await request.json()
    try:
        job_input = validate_job_payload(payload)
    except ValueError as exc:
        if str(exc) == "invalid_product":
            return error_response(400, "invalid_product")
        return error_response(400, "invalid_product_contract", detail=str(exc))

    job_id = str(uuid.uuid4())
    idempotency_key = job_input["idempotency_key"]

    with connect() as conn:
        with conn.cursor() as cur:
            if idempotency_key:
                cur.execute(
                    """
                    insert into jobs(id, status, count, product, idempotency_key, profile)
                    values (%s, 'queued', %s, %s, %s, %s)
                    on conflict (idempotency_key) do nothing
                    returning *
                    """,
                    (
                        job_id,
                        job_input["count"],
                        job_input["product"],
                        idempotency_key,
                        Jsonb(PRODUCTION_PROFILE),
                    ),
                )
                job = cur.fetchone()
                if not job:
                    cur.execute("select * from jobs where idempotency_key = %s", (idempotency_key,))
                    existing = cur.fetchone()
                    return {
                        "success": True,
                        "status": existing["status"],
                        "idempotent": True,
                        "job": public_job(existing),
                    }
            else:
                cur.execute(
                    """
                    insert into jobs(id, status, count, product, profile)
                    values (%s, 'queued', %s, %s, %s)
                    returning *
                    """,
                    (job_id, job_input["count"], job_input["product"], Jsonb(PRODUCTION_PROFILE)),
                )
                job = cur.fetchone()
        log_job_event(
            conn,
            job["id"],
            "queued",
            {"profile": PRODUCTION_PROFILE, "ipv6_policy": "ipv6_only", "idempotency_key": idempotency_key},
        )

    logger.info(
        "job queued job_id=%s idempotency_key=%s profile=%s ipv6_policy=ipv6_only",
        job["id"],
        idempotency_key,
        PRODUCTION_PROFILE["fingerprint_profile_version"],
    )
    return {"success": True, "status": "queued", "idempotent": False, "job": public_job(job)}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job(job_id: str):
    row = fetch_one("select * from jobs where id = %s", (job_id,))
    if not row:
        return error_response(404, "job_not_found")
    return {"success": True, "status": row["status"], "job": public_job(row)}


@app.get("/jobs/{job_id}/proxies.list", dependencies=[Depends(require_api_key)])
def download_proxies(job_id: str):
    row = fetch_one("select * from jobs where id = %s", (job_id,))
    if not row:
        return error_response(404, "job_not_found")
    if row["status"] != "success" or not row.get("result_path"):
        return error_response(409, "job_not_ready")
    path = Path(row["result_path"])
    if not path.exists():
        return error_response(404, "proxies_list_not_found")
    return FileResponse(path, media_type="text/plain", filename="proxies.list")
