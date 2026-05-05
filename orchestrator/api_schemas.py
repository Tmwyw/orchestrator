"""Pydantic v2 API request/response schemas for orchestrator endpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.schemas import OrderStatus

_API_MODEL_CONFIG = ConfigDict(str_strip_whitespace=True, extra="forbid")


def _coerce_decimal(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid decimal: {value!r}") from exc
    return value


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


# === /v1/nodes/enroll (Wave B-6.2) ===


class EnrollRequest(BaseModel):
    model_config = _API_MODEL_CONFIG

    agent_url: str = Field(min_length=8, max_length=512)
    api_key: str | None = Field(default=None, max_length=128)
    name: str | None = Field(default=None, max_length=128)
    geo_code: str | None = Field(default=None, max_length=8)
    force: bool = False
    auto_bind_active_skus: bool = False


class NodeSummary(BaseModel):
    model_config = _API_MODEL_CONFIG

    id: str
    name: str
    url: str
    geo: str
    status: str
    capacity: int
    runtime_status: str | None = None


class EnrollResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = True
    node: NodeSummary
    describe_geo_code: str | None = None
    auto_bound_skus: list[str] = Field(default_factory=list)


# === /v1/admin/* (Wave B-7b.3) ===


class StatsSales(BaseModel):
    orders: int
    proxies: int
    revenue: Decimal


class StatsInventoryRow(BaseModel):
    code: str
    status: str
    n: int


class StatsNodes(BaseModel):
    ready: int
    total: int


class PergbTopSku(BaseModel):
    """One row of /v1/admin/stats.pergb.top_skus_by_revenue_7d (B-8.3)."""

    model_config = _API_MODEL_CONFIG

    sku_code: str
    revenue: Decimal
    accounts: int

    @field_validator("revenue", mode="before")
    @classmethod
    def _parse_decimal(cls, v: Any) -> Any:
        return _coerce_decimal(v)


class PergbStatsSubsection(BaseModel):
    """Pay-per-GB summary on /v1/admin/stats (B-8.3)."""

    model_config = _API_MODEL_CONFIG

    active_accounts: int
    depleted_accounts: int
    expired_accounts: int
    bytes_consumed_7d: int
    top_skus_by_revenue_7d: list[PergbTopSku]


class StatsResponse(BaseModel):
    sales: StatsSales
    inventory: list[StatsInventoryRow]
    nodes: StatsNodes
    pergb: PergbStatsSubsection | None = None


class OrderListItem(BaseModel):
    order_ref: str
    user_id: int
    sku_id: int
    status: str
    requested_count: int
    allocated_count: int
    reserved_at: datetime
    expires_at: datetime
    committed_at: datetime | None = None
    proxies_expires_at: datetime | None = None


class OrdersListResponse(BaseModel):
    items: list[OrderListItem]
    count: int


class ArchiveExportItem(BaseModel):
    id: int
    sku_code: str
    host: str
    port: int
    login: str
    password: str
    geo_country: str | None = None
    archived_at: datetime
    order_id: int | None = None


class ArchiveExportResponse(BaseModel):
    items: list[ArchiveExportItem]
    count: int
    from_date: str = Field(alias="from")
    to_date: str = Field(alias="to")


# === Pay-per-GB (Wave B-8) ===

# Schema for skus.metadata['tiers'] when product_kind = datacenter_pergb.
# Per docs/wave_b8_design.md § 2.4 + § 5.1; Decimal serialized as JSON
# string per § 6.10 money convention.


class SkuTier(BaseModel):
    model_config = _API_MODEL_CONFIG

    gb: int = Field(ge=1)
    price_per_gb: Decimal

    @field_validator("price_per_gb", mode="before")
    @classmethod
    def _parse_decimal(cls, v: Any) -> Any:
        return _coerce_decimal(v)

    @field_validator("price_per_gb")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("price_per_gb must be > 0")
        return v


class SkuTierTable(BaseModel):
    model_config = _API_MODEL_CONFIG

    tiers: list[SkuTier] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_sorted_unique(self) -> SkuTierTable:
        prev_gb = 0
        for tier in self.tiers:
            if tier.gb <= prev_gb:
                # Duplicates land here too (gb == prev_gb).
                raise ValueError("tiers must be sorted ascending by gb (no duplicates)")
            prev_gb = tier.gb
        return self


class ReservePergbRequest(BaseModel):
    model_config = _API_MODEL_CONFIG

    user_id: int = Field(gt=0)
    sku_id: int = Field(gt=0)
    gb_amount: int = Field(ge=1)
    idempotency_key: str | None = Field(default=None, max_length=128)


class ReservePergbResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = True
    order_ref: str
    expires_at: datetime
    port: int
    host: str
    login: str
    password: str
    bytes_quota: int
    price_amount: Decimal

    @field_validator("price_amount", mode="before")
    @classmethod
    def _parse_decimal(cls, v: Any) -> Any:
        return _coerce_decimal(v)


class TopupPergbRequest(BaseModel):
    model_config = _API_MODEL_CONFIG

    sku_id: int = Field(gt=0)
    gb_amount: int = Field(ge=1)
    idempotency_key: str | None = Field(default=None, max_length=128)


class TopupPergbResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = True
    order_ref: str
    parent_order_ref: str
    topup_sequence: int
    bytes_quota_total: int
    bytes_used: int
    expires_at: datetime
    price_amount: Decimal
    tier_price_per_gb: Decimal

    @field_validator("price_amount", "tier_price_per_gb", mode="before")
    @classmethod
    def _parse_decimal(cls, v: Any) -> Any:
        return _coerce_decimal(v)


class TrafficResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    order_ref: str
    status: str
    bytes_quota: int
    bytes_used: int
    bytes_remaining: int
    usage_pct: float
    last_polled_at: datetime | None = None
    expires_at: datetime
    depleted_at: datetime | None = None
    node_id: str
    port: int
    over_usage_bytes: int = 0


class AdminTrafficPollResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    accounts_polled: int
    nodes_polled: int
    bytes_observed_total: int
    counter_resets_detected: int
    accounts_marked_depleted: int
