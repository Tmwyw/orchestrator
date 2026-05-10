"""Pay-per-GB FastAPI router (Wave B-8).

Three endpoints land real handlers in B-8.2 backed by ``PergbService``:

- ``POST /v1/orders/reserve_pergb`` — purchase a pergb account.
- ``POST /v1/orders/{order_ref}/topup_pergb`` — extend quota + lease on an
  active or depleted account.
- ``GET /v1/orders/{order_ref}/traffic`` — snapshot for the bot's poller.

``POST /v1/admin/traffic/poll`` lives in ``orchestrator/admin.py`` (B-8.3).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from orchestrator.api_schemas import (
    GeneratedPort,
    GeneratePortsRequest,
    GeneratePortsResponse,
    ReservePergbRequest,
    ReservePergbResponse,
    SkuTierTable,
    TopupPergbRequest,
    TopupPergbResponse,
    TrafficResponse,
)
from orchestrator.pergb_service import PergbService

pergb_router = APIRouter()
_service = PergbService()


def validate_pergb_metadata(metadata: Any) -> SkuTierTable:
    """Validate skus.metadata for product_kind=datacenter_pergb.

    Raises pydantic.ValidationError on malformed tiers; returns the parsed
    SkuTierTable on success. Wired into admin SKU CRUD in B-8.2 — exposed
    here so call sites can do ``from orchestrator.pergb import ...``.

    Expected shape: ``{"tiers": [{"gb": int, "price_per_gb": "9.99"}, ...]}``.
    """
    return SkuTierTable.model_validate(metadata)


# === error helpers ===


_RESERVE_ERROR_STATUS: dict[str, int] = {
    "sku_not_found": 404,
    "sku_not_pergb": 400,
    "sku_tiers_invalid": 500,
    "invalid_tier_amount": 400,
}

_GENERATE_PORTS_ERROR_STATUS: dict[str, int] = {
    "order_not_found": 404,
    "account_not_active": 409,
    "insufficient_pool": 409,
}

_TOPUP_ERROR_STATUS: dict[str, int] = {
    "order_not_found": 404,
    "sku_mismatch_for_topup": 400,
    "sku_not_found": 404,
    "sku_tiers_invalid": 500,
    "invalid_tier_amount": 400,
    "account_not_renewable": 409,
    "duplicate_idempotency_key": 409,
}

_TRAFFIC_ERROR_STATUS: dict[str, int] = {
    "order_not_found": 404,
    "traffic_account_not_found": 404,
}


def _error_response(*, status: int, error: str, detail: str | None = None, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"success": False, "error": error}
    if detail is not None:
        body["detail"] = detail
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


# === endpoints ===


@pergb_router.post("/v1/orders/reserve_pergb")
async def reserve_pergb(payload: ReservePergbRequest) -> JSONResponse:
    result = await _service.reserve_pergb(
        user_id=payload.user_id,
        sku_id=payload.sku_id,
        gb_amount=payload.gb_amount,
        idempotency_key=payload.idempotency_key,
    )
    if not result.success:
        status = _RESERVE_ERROR_STATUS.get(result.error or "", 500)
        extra: dict[str, Any] = {}
        if result.error == "invalid_tier_amount" and result.available_tiers is not None:
            extra["available_tiers"] = result.available_tiers
        return _error_response(status=status, error=result.error or "unknown", **extra)

    assert result.order_ref is not None
    assert result.expires_at is not None
    assert result.traffic_account_id is not None
    response = ReservePergbResponse(
        order_ref=result.order_ref,
        expires_at=result.expires_at,
        bytes_quota=int(result.bytes_quota or 0),
        price_amount=result.price_amount or 0,  # type: ignore[arg-type]
        traffic_account_id=result.traffic_account_id,
    )
    return JSONResponse(content=response.model_dump(mode="json"))


@pergb_router.post("/v1/pergb/{order_ref}/generate_ports")
async def generate_ports(order_ref: str, payload: GeneratePortsRequest) -> JSONResponse:
    result = await _service.generate_ports(
        order_ref=order_ref,
        count=payload.count,
        geo_code=payload.geo_code,
        idempotency_key=payload.idempotency_key,
    )
    if not result.success:
        status = _GENERATE_PORTS_ERROR_STATUS.get(result.error or "", 500)
        extra: dict[str, Any] = {}
        if result.error == "insufficient_pool":
            extra["available"] = int(result.available or 0)
            extra["requested"] = int(result.requested or 0)
            extra["geo_code"] = result.geo_code or ""
        if result.error == "account_not_active" and result.current_status:
            extra["current_status"] = result.current_status
        return _error_response(status=status, error=result.error or "unknown", **extra)

    assert result.order_ref is not None
    assert result.traffic_account_id is not None
    response = GeneratePortsResponse(
        order_ref=result.order_ref,
        traffic_account_id=result.traffic_account_id,
        ports=[
            GeneratedPort(
                port=p.port,
                host=p.host,
                login=p.login,
                password=p.password,
                geo_code=p.geo_code,
            )
            for p in (result.ports or [])
        ],
        total_ports_for_client=int(result.total_ports_for_client or 0),
    )
    return JSONResponse(content=response.model_dump(mode="json"))


@pergb_router.post("/v1/orders/{order_ref}/topup_pergb")
async def topup_pergb(order_ref: str, payload: TopupPergbRequest) -> JSONResponse:
    result = await _service.topup_pergb(
        parent_order_ref=order_ref,
        sku_id=payload.sku_id,
        gb_amount=payload.gb_amount,
        idempotency_key=payload.idempotency_key,
    )
    if not result.success:
        status = _TOPUP_ERROR_STATUS.get(result.error or "", 500)
        extra: dict[str, Any] = {}
        if result.error == "invalid_tier_amount" and result.available_tiers is not None:
            extra["available_tiers"] = result.available_tiers
        if result.error == "account_not_renewable" and result.current_status:
            extra["current_status"] = result.current_status
        return _error_response(status=status, error=result.error or "unknown", **extra)

    assert result.order_ref is not None
    assert result.expires_at is not None
    response = TopupPergbResponse(
        order_ref=result.order_ref,
        parent_order_ref=result.parent_order_ref or "",
        topup_sequence=int(result.topup_sequence or 0),
        bytes_quota_total=int(result.bytes_quota_total or 0),
        bytes_used=int(result.bytes_used or 0),
        expires_at=result.expires_at,
        price_amount=result.price_amount or 0,  # type: ignore[arg-type]
        tier_price_per_gb=result.tier_price_per_gb or 0,  # type: ignore[arg-type]
    )
    return JSONResponse(content=response.model_dump(mode="json"))


@pergb_router.get("/v1/orders/{order_ref}/traffic")
async def get_traffic(order_ref: str) -> JSONResponse:
    result = await _service.get_traffic(parent_order_ref=order_ref)
    if not result.success:
        status = _TRAFFIC_ERROR_STATUS.get(result.error or "", 500)
        return _error_response(
            status=status,
            error=result.error or "unknown",
            detail=result.detail,
        )

    assert result.order_ref is not None
    assert result.expires_at is not None
    response = TrafficResponse(
        order_ref=result.order_ref,
        status=result.status or "",
        bytes_quota=int(result.bytes_quota or 0),
        bytes_used=int(result.bytes_used or 0),
        bytes_remaining=int(result.bytes_remaining or 0),
        usage_pct=float(result.usage_pct or 0.0),
        last_polled_at=result.last_polled_at,
        expires_at=result.expires_at,
        depleted_at=result.depleted_at,
        node_id=result.node_id,
        port=result.port,
        over_usage_bytes=int(result.over_usage_bytes or 0),
    )
    return JSONResponse(content=response.model_dump(mode="json"))


# NOTE: /v1/admin/traffic/poll moved to orchestrator/admin.py admin_router
# in B-8.3 (was a 501 stub here in B-8.1, now real). All other admin endpoints
# live there; the route follows the prefix convention.
