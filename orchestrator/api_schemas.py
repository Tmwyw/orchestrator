"""Pydantic v2 API request/response schemas for orchestrator endpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.schemas import OrderStatus

_API_MODEL_CONFIG = ConfigDict(str_strip_whitespace=True, extra="forbid")


# === /v1/orders/reserve ===


class ReserveRequest(BaseModel):
    model_config = _API_MODEL_CONFIG

    user_id: int = Field(gt=0)
    sku_id: int = Field(gt=0)
    quantity: int = Field(ge=1, le=50_000)
    reservation_ttl_sec: int = Field(default=300, ge=30, le=3600)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ReserveResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = True
    order_ref: str
    expires_at: datetime
    proxies_count: int
    proxies_url: str


class ReserveErrorResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = False
    error: str
    available_now: int | None = None
    detail: str | None = None


# === /v1/orders/{ref}/commit ===


class CommitRequest(BaseModel):
    model_config = _API_MODEL_CONFIG

    duration_days: int | None = Field(default=None, ge=1, le=365)


class CommitResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = True
    order_ref: str
    status: OrderStatus
    proxies_expires_at: datetime
    proxies_url: str


# === /v1/orders/{ref}/release ===


class ReleaseResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = True
    order_ref: str
    status: OrderStatus
    released_count: int


# === /v1/orders/{ref} (GET) ===


class OrderResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    order_ref: str
    user_id: int
    sku_id: int
    status: OrderStatus
    requested_count: int
    allocated_count: int
    reserved_at: datetime
    expires_at: datetime
    committed_at: datetime | None = None
    released_at: datetime | None = None
    proxies_expires_at: datetime | None = None
    price_amount: Decimal | None = None


# === /v1/orders/{ref}/extend ===


class ExtendRequest(BaseModel):
    model_config = _API_MODEL_CONFIG

    duration_days: int = Field(ge=1, le=365)
    inventory_ids: list[int] | None = Field(default=None, max_length=50_000)
    geo_code: str | None = Field(default=None, min_length=2, max_length=8)

    @model_validator(mode="after")
    def _check_selectors_mutually_exclusive(self) -> ExtendRequest:
        if self.inventory_ids is not None and self.geo_code is not None:
            raise ValueError("inventory_ids and geo_code are mutually exclusive")
        return self


class ExtendResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = True
    order_ref: str
    extended_count: int
    new_proxies_expires_at: datetime


# === /v1/orders/{ref}/proxies ===


class ProxiesErrorResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = False
    error: str
    locked_format: str | None = None
    detail: str | None = None


# === Generic error (RFC 7807-style) ===


class ProblemResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = False
    error: str
    detail: str | None = None
    extra: dict[str, Any] | None = None
