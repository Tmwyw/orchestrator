import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from psycopg.types.json import Jsonb

from orchestrator import node_client
from orchestrator.allocator import AllocatorService
from orchestrator.api_schemas import (
    CommitRequest,
    CommitResponse,
    EnrollRequest,
    EnrollResponse,
    ExtendRequest,
    ExtendResponse,
    NodeSummary,
    OrderResponse,
    ProblemResponse,
    ProxiesErrorResponse,
    ReleaseResponse,
    ReserveErrorResponse,
    ReserveRequest,
    ReserveResponse,
)
from orchestrator.config import get_config
from orchestrator.db import connect, fetch_all, fetch_one
from orchestrator.jobs import log_job_event, node_health_diagnostics, node_health_ready, public_job
from orchestrator.logging_setup import configure_logging, get_logger
from orchestrator.metrics import HTTP_DURATION_SEC, HTTP_REQUESTS
from orchestrator.node_client import check_health
from orchestrator.schemas import DeliveryFormat
from shared.contracts import FORBIDDEN_JOB_FIELDS, PRODUCTION_PROFILE

configure_logging()
logger = get_logger("netrun-orchestrator")

app = FastAPI(
    title="NETRUN Orchestrator",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def track_http(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    duration = time.monotonic() - start
    route = request.scope.get("route")
    path = route.path if route is not None else request.url.path
    HTTP_REQUESTS.labels(
        method=request.method,
        path=path,
        status=str(response.status_code),
    ).inc()
    HTTP_DURATION_SEC.labels(method=request.method, path=path).observe(duration)
    return response


# /metrics intentionally has no API key — protected by network boundary
# (bind 127.0.0.1 + optional nginx ACL via scripts/install_nginx.sh in
# B-7b.5). DO NOT expose 8090 publicly.
@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


ALLOWED_PRODUCTS = {"android_ipv6_only", "smoke"}
ALLOWED_JOB_FIELDS = {"count", "product", "idempotency_key"}
ALLOWED_NODE_FIELDS = {"id", "name", "url", "geo", "capacity", "api_key", "force"}

_allocator = AllocatorService()


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
                return error_response(
                    409, "node_unavailable", node_health=node_health_diagnostics(node_health)
                )
            node_status = "unavailable"

    with connect() as conn, conn.cursor() as cur:
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
    with connect() as conn, conn.cursor() as cur:
        cur.execute("delete from nodes where id = %s returning id", (node_id,))
        row = cur.fetchone()
    if not row:
        return error_response(404, "node_not_found")
    return {"success": True, "status": "ready", "deleted": node_id}


@app.post("/v1/nodes/enroll", dependencies=[Depends(require_api_key)])
async def enroll_node(payload: EnrollRequest):
    """Auto-enroll a node by fetching its /describe and validating /health."""
    url = payload.agent_url.rstrip("/")
    node_api_key: str | None = payload.api_key.strip() if payload.api_key else None
    if node_api_key == "":
        node_api_key = None

    try:
        describe = await asyncio.to_thread(node_client.describe, url, node_api_key, 15)
    except Exception as exc:
        return JSONResponse(
            status_code=502,
            content=ProblemResponse(error="describe_unreachable", detail=str(exc)).model_dump(
                exclude_none=True, mode="json"
            ),
        )

    if describe.get("api_key_required") and not node_api_key:
        return JSONResponse(
            status_code=400,
            content=ProblemResponse(error="api_key_required_by_node").model_dump(
                exclude_none=True, mode="json"
            ),
        )

    try:
        health = await asyncio.to_thread(check_health, url, node_api_key, 10)
    except Exception as exc:
        if not payload.force:
            return JSONResponse(
                status_code=409,
                content=ProblemResponse(error="health_unreachable", detail=str(exc)).model_dump(
                    exclude_none=True, mode="json"
                ),
            )
        node_status = "unavailable"
    else:
        if not node_health_ready(health):
            if not payload.force:
                return JSONResponse(
                    status_code=409,
                    content=ProblemResponse(
                        error="node_health_not_ready",
                        extra={"diagnostics": node_health_diagnostics(health)},
                    ).model_dump(exclude_none=True, mode="json"),
                )
            node_status = "unavailable"
        else:
            node_status = "ready"

    geo = ((payload.geo_code or describe.get("geo_code") or "") or "").strip()
    capacity = int(describe.get("capacity") or 1000)
    name = (payload.name or "").strip()
    url_hash = uuid.uuid5(uuid.NAMESPACE_URL, url).hex[:8]
    if not name:
        name = f"node-{geo.lower()}-{url_hash}" if geo else f"node-{url_hash}"
    node_id = uuid.uuid5(uuid.NAMESPACE_URL, url).hex

    generator_script = describe.get("generator_script") or ""
    max_parallel = int(describe.get("max_parallel_jobs") or 1)
    max_batch = int(describe.get("max_batch_size") or 1500)

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            insert into nodes (
              id, name, url, geo, status, capacity, api_key,
              generator_script, max_parallel_jobs, max_batch_size,
              runtime_status, last_health_check
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', now())
            on conflict (url) do update set
              name = excluded.name,
              geo = excluded.geo,
              capacity = excluded.capacity,
              api_key = excluded.api_key,
              generator_script = excluded.generator_script,
              max_parallel_jobs = excluded.max_parallel_jobs,
              max_batch_size = excluded.max_batch_size,
              status = excluded.status,
              last_health_check = now(),
              updated_at = now()
            returning *
            """,
            (
                node_id,
                name,
                url,
                geo,
                node_status,
                capacity,
                node_api_key,
                generator_script,
                max_parallel,
                max_batch,
            ),
        )
        node_row = cur.fetchone() or {}

    actual_node_id = str(node_row.get("id") or node_id)
    actual_name = str(node_row.get("name") or name)

    bound_skus: list[str] = []
    if payload.auto_bind_active_skus and geo:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into sku_node_bindings (sku_id, node_id, weight, max_batch_size, is_active)
                select s.id, %s, 100, least(%s, %s), true
                from skus s
                where s.is_active = true and s.geo_code = %s
                on conflict (sku_id, node_id) do update set
                  is_active = true,
                  updated_at = now()
                returning (select code from skus where id = sku_node_bindings.sku_id) as code
                """,
                (actual_node_id, max_batch, capacity, geo),
            )
            bound_skus = [r["code"] for r in cur.fetchall() if r.get("code")]

    logger.info(
        "main_node_enrolled",
        node_id=actual_node_id,
        name=actual_name,
        url=url,
        geo=geo,
        status=node_status,
        auto_bound_skus=bound_skus,
    )
    return JSONResponse(
        content=EnrollResponse(
            node=NodeSummary(
                id=actual_node_id,
                name=actual_name,
                url=url,
                geo=geo,
                status=node_status,
                capacity=capacity,
                runtime_status="active",
            ),
            describe_geo_code=describe.get("geo_code"),
            auto_bound_skus=bound_skus,
        ).model_dump(mode="json"),
    )


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
        "main_job_queued",
        job_id=job["id"],
        idempotency_key=idempotency_key,
        fingerprint_profile=PRODUCTION_PROFILE["fingerprint_profile_version"],
        ipv6_policy="ipv6_only",
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


# === /v1/orders endpoints (Wave B-4a) ===


@app.post("/v1/orders/reserve", dependencies=[Depends(require_api_key)])
async def reserve_order(payload: ReserveRequest):
    result = await _allocator.reserve(
        user_id=payload.user_id,
        sku_id=payload.sku_id,
        quantity=payload.quantity,
        reservation_ttl_sec=payload.reservation_ttl_sec,
        idempotency_key=payload.idempotency_key,
    )
    if not result.success:
        status_code = 409 if result.error == "insufficient_stock" else 400
        return JSONResponse(
            status_code=status_code,
            content=ReserveErrorResponse(
                error=result.error or "unknown",
                available_now=result.available_now,
            ).model_dump(exclude_none=True, mode="json"),
        )
    assert result.order_ref is not None and result.expires_at is not None
    return JSONResponse(
        content=ReserveResponse(
            order_ref=result.order_ref,
            expires_at=result.expires_at,
            proxies_count=result.proxies_count,
            proxies_url=f"/v1/orders/{result.order_ref}/proxies",
        ).model_dump(mode="json"),
    )


@app.post("/v1/orders/{order_ref}/commit", dependencies=[Depends(require_api_key)])
async def commit_order(order_ref: str, payload: CommitRequest):
    result = await _allocator.commit(order_ref=order_ref, duration_days=payload.duration_days)
    if not result.success:
        status_code = 404 if result.error == "order_not_found" else 409
        return JSONResponse(
            status_code=status_code,
            content=ProblemResponse(error=result.error or "unknown").model_dump(
                exclude_none=True, mode="json"
            ),
        )
    assert result.proxies_expires_at is not None
    return JSONResponse(
        content=CommitResponse(
            order_ref=result.order_ref,
            status=result.status,
            proxies_expires_at=result.proxies_expires_at,
            proxies_url=f"/v1/orders/{result.order_ref}/proxies",
        ).model_dump(mode="json"),
    )


@app.post("/v1/orders/{order_ref}/release", dependencies=[Depends(require_api_key)])
async def release_order(order_ref: str):
    result = await _allocator.release(order_ref=order_ref)
    if not result.success:
        status_code = 404 if result.error == "order_not_found" else 409
        return JSONResponse(
            status_code=status_code,
            content=ProblemResponse(error=result.error or "unknown").model_dump(
                exclude_none=True, mode="json"
            ),
        )
    return JSONResponse(
        content=ReleaseResponse(
            order_ref=result.order_ref,
            status=result.status,
            released_count=result.released_count,
        ).model_dump(mode="json"),
    )


@app.get("/v1/orders/{order_ref}", dependencies=[Depends(require_api_key)])
async def get_order(order_ref: str):
    order = await asyncio.to_thread(_allocator._sync_get_order, order_ref)
    if not order:
        return JSONResponse(
            status_code=404,
            content=ProblemResponse(error="order_not_found").model_dump(exclude_none=True, mode="json"),
        )
    return JSONResponse(content=OrderResponse.model_validate(order).model_dump(mode="json"))


@app.get("/v1/orders/{order_ref}/proxies", dependencies=[Depends(require_api_key)])
async def get_order_proxies(order_ref: str, format: str = "socks5_uri"):
    try:
        format_enum = DeliveryFormat(format)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content=ProblemResponse(error="invalid_format", detail=f"format={format}").model_dump(
                exclude_none=True, mode="json"
            ),
        )

    result = await _allocator.get_proxies(order_ref=order_ref, format=format_enum)
    if not result.success:
        status_code = 404 if result.error in ("order_not_found", "inventory_empty") else 409
        return JSONResponse(
            status_code=status_code,
            content=ProxiesErrorResponse(
                error=result.error or "unknown",
                locked_format=result.locked_format,
            ).model_dump(exclude_none=True, mode="json"),
        )

    headers = {"X-Line-Count": str(result.line_count)}
    return Response(
        content=result.content,
        media_type=result.content_type,
        headers=headers,
    )


@app.post("/v1/orders/{order_ref}/extend", dependencies=[Depends(require_api_key)])
async def extend_order_endpoint(order_ref: str, payload: ExtendRequest):
    result = await _allocator.extend_order(
        order_ref=order_ref,
        duration_days=payload.duration_days,
        inventory_ids=payload.inventory_ids,
        geo_code=payload.geo_code,
    )
    if not result.success:
        status_code = 404 if result.error == "order_not_found" else 409
        return JSONResponse(
            status_code=status_code,
            content=ProblemResponse(error=result.error or "unknown").model_dump(
                exclude_none=True, mode="json"
            ),
        )
    assert result.new_proxies_expires_at is not None
    return JSONResponse(
        content=ExtendResponse(
            order_ref=result.order_ref,
            extended_count=result.extended_count,
            new_proxies_expires_at=result.new_proxies_expires_at,
        ).model_dump(mode="json"),
    )


# === /v1/* aliases for legacy endpoints (Wave B-7a) ===
# Old paths (/health, /nodes, /jobs) remain wired for backward compatibility
# and will be removed in the next major version. Sale-domain (/v1/orders/*,
# /v1/nodes/enroll) is unaffected and not duplicated here.

v1_router = APIRouter(prefix="/v1")
v1_router.add_api_route("/health", health, methods=["GET"], dependencies=[Depends(require_api_key)])
v1_router.add_api_route("/nodes", list_nodes, methods=["GET"], dependencies=[Depends(require_api_key)])
v1_router.add_api_route("/nodes", create_node, methods=["POST"], dependencies=[Depends(require_api_key)])
v1_router.add_api_route(
    "/nodes/{node_id}",
    delete_node,
    methods=["DELETE"],
    dependencies=[Depends(require_api_key)],
)
v1_router.add_api_route("/jobs", create_job, methods=["POST"], dependencies=[Depends(require_api_key)])
v1_router.add_api_route("/jobs/{job_id}", get_job, methods=["GET"], dependencies=[Depends(require_api_key)])
v1_router.add_api_route(
    "/jobs/{job_id}/proxies.list",
    download_proxies,
    methods=["GET"],
    dependencies=[Depends(require_api_key)],
)

app.include_router(v1_router)
