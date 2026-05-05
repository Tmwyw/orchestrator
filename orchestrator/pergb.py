"""Pay-per-GB endpoint stubs for Wave B-8.1.

All four endpoints return 501 not_implemented. Real implementation lands
in B-8.2. Pydantic request validation is fully wired (422 on malformed
body); 501 fires after validation passes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from orchestrator.api_schemas import (
    AdminTrafficPollResponse,  # noqa: F401  (used in B-8.2)
    ReservePergbRequest,
    ReservePergbResponse,  # noqa: F401  (used in B-8.2)
    SkuTierTable,
    TopupPergbRequest,
    TopupPergbResponse,  # noqa: F401  (used in B-8.2)
    TrafficResponse,  # noqa: F401  (used in B-8.2)
)

pergb_router = APIRouter()


def validate_pergb_metadata(metadata: Any) -> SkuTierTable:
    """Validate skus.metadata for product_kind=datacenter_pergb.

    Raises pydantic.ValidationError on malformed tiers; returns the parsed
    SkuTierTable on success. Wired into admin SKU CRUD in B-8.2 — exposed
    here so call sites can do `from orchestrator.pergb import ...`.

    Expected shape: ``{"tiers": [{"gb": int, "price_per_gb": "9.99"}, ...]}``.
    """
    return SkuTierTable.model_validate(metadata)


NOT_IMPLEMENTED_BODY = {
    "success": False,
    "error": "not_implemented",
    "detail": "Wave B-8.2 will land the implementation",
}


@pergb_router.post("/v1/orders/reserve_pergb")
async def reserve_pergb(payload: ReservePergbRequest) -> JSONResponse:
    return JSONResponse(status_code=501, content=NOT_IMPLEMENTED_BODY)


@pergb_router.post("/v1/orders/{order_ref}/topup_pergb")
async def topup_pergb(order_ref: str, payload: TopupPergbRequest) -> JSONResponse:
    return JSONResponse(status_code=501, content=NOT_IMPLEMENTED_BODY)


@pergb_router.get("/v1/orders/{order_ref}/traffic")
async def get_traffic(order_ref: str) -> JSONResponse:
    return JSONResponse(status_code=501, content=NOT_IMPLEMENTED_BODY)


@pergb_router.post("/v1/admin/traffic/poll")
async def admin_traffic_poll() -> JSONResponse:
    return JSONResponse(status_code=501, content=NOT_IMPLEMENTED_BODY)
