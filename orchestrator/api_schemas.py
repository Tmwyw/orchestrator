"""Pydantic v2 API request/response schemas for orchestrator endpoints."""

from __future__ import annotations

import re
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
    # extra="ignore" because this model consumes DB rows directly via
    # OrderResponse.model_validate(order) and the orders table has more
    # columns (id, reservation_key, idempotency_key, metadata, created_at,
    # updated_at) than we expose on the wire.
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

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


class InstallResultIn(BaseModel):
    # Tolerant of future fields the node-side cloud-init might add.
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    ok: bool
    exit_code: int = 0
    log_tail: str = ""


class RegisterRequest(BaseModel):
    """Body of POST /v1/nodes/register (contract fixed by Промпт ①)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    ip: str = Field(min_length=3, max_length=64)
    secret: str = Field(min_length=8, max_length=256)
    install_result: InstallResultIn
    hostname: str = Field(default="", max_length=255)
    agent_version: str = Field(default="", max_length=64)


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
    """Reserve_pergb response — Wave PERGB-RFCT-A.

    No port credentials any more: reserve_pergb only creates the
    traffic_account (the GB budget). The bot then calls
    POST /v1/pergb/{order_ref}/generate_ports to claim N pool ports
    lazily.
    """

    model_config = _API_MODEL_CONFIG

    success: bool = True
    order_ref: str
    expires_at: datetime
    bytes_quota: int
    price_amount: Decimal
    traffic_account_id: int

    @field_validator("price_amount", mode="before")
    @classmethod
    def _parse_decimal(cls, v: Any) -> Any:
        return _coerce_decimal(v)


class GeneratePortsRequest(BaseModel):
    model_config = _API_MODEL_CONFIG

    # Bot's pergb_panel._GENERATE_MAX_COUNT = 500. Server-side cap must match
    # or 422 rejects every batch > old limit. With parallelize sem=20 (inside
    # pergb_service.generate_ports) + 300s bot HTTP timeout in place, 500
    # ports complete in 30-50s comfortably.
    count: int = Field(ge=1, le=500, description="Ports to allocate from pool")
    geo_code: str = Field(min_length=2, max_length=10)
    idempotency_key: str = Field(min_length=8, max_length=128)


class GeneratedPort(BaseModel):
    model_config = _API_MODEL_CONFIG

    port: int
    host: str
    login: str
    password: str
    geo_code: str


class GeneratePortsResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = True
    order_ref: str
    traffic_account_id: int
    ports: list[GeneratedPort]
    total_ports_for_client: int


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
    """Traffic snapshot. node_id/port are optional after Wave PERGB-RFCT-A
    — a fresh pergb account has no ports until generate_ports is called.
    """

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
    node_id: str | None = None
    port: int | None = None
    over_usage_bytes: int = 0
    port_count: int = 0


class AdminTrafficPollResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    accounts_polled: int
    nodes_polled: int
    bytes_observed_total: int
    counter_resets_detected: int
    accounts_marked_depleted: int


# === Wave PER-USER-TOOLS-1 — admin SET quota + change-expiry =====


class AdminSetQuotaRequest(BaseModel):
    """Body for PATCH /v1/admin/orders/{ref}/quota.

    SET semantics (NOT topup-add) — replaces ``bytes_quota`` with
    ``round(gb_amount * 1024**3)``. ``bytes_used`` is preserved; the
    status recomputes (active vs depleted) against the new quota."""

    model_config = _API_MODEL_CONFIG

    gb_amount: float = Field(gt=0, le=100_000)


class AdminSetQuotaResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    order_ref: str
    bytes_quota: int
    bytes_used: int
    bytes_remaining: int
    status: str
    expires_at: datetime


class AdminChangeExpiryRequest(BaseModel):
    """Body for PATCH /v1/admin/orders/{ref}/expiry.

    ``mode`` selects the operation:
      * ``add`` — new = current + days (NULL current → now() + days)
      * ``set`` — new = now() + days (replaces unconditionally)
      * ``subtract`` — new = current - days (422 if new < now(); 409 if NULL current)
    """

    model_config = _API_MODEL_CONFIG

    mode: str = Field(pattern=r"^(add|set|subtract)$")
    days: int = Field(ge=1, le=365)


class AdminChangeExpiryResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    order_ref: str
    mode: str
    days: int
    old_expires_at: datetime | None
    new_expires_at: datetime
    affected_inventory_count: int


# === /v1/skus/active (Wave B catalog endpoint) ===


class SkuItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    sku_id: int
    code: str
    geo_code: str
    name: str
    price_per_unit: Decimal
    stock_available: int
    duration_days: int
    product_kind: str = "ipv6_per_piece"
    tiers: list[dict[str, Any]] | None = None


class SkusListResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    success: bool = True
    items: list[SkuItem]
    count: int


# === /v1/admin/skus, /v1/admin/skus/{id}/* (CATALOG-1 Phase A) ===


class SkuAdminItem(BaseModel):
    """Row in GET /v1/admin/skus list. Includes inactive SKUs.

    ``display_name`` is computed at query time (see
    ``admin_catalog._compute_display_name``) — emoji + kind label + geo
    + protocol + duration — so the bot can render SKU buttons / cards
    without maintaining its own copy of the label / flag dictionaries.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    id: int
    code: str
    product_kind: str
    geo_code: str
    protocol: str
    duration_days: int
    price_per_piece: Decimal | None = None
    price_per_gb: Decimal | None = None
    target_stock: int
    refill_batch_size: int
    is_active: bool
    stock_available: int
    display_name: str
    created_at: datetime
    updated_at: datetime


class SkuListResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    items: list[SkuAdminItem]
    total: int


class SkuStockBreakdownItem(BaseModel):
    """Per-node stock counts on GET /v1/admin/skus/{id}."""

    model_config = _API_MODEL_CONFIG

    node_id: str
    node_name: str
    available: int
    reserved: int
    sold: int
    expired_grace: int
    pending_validation: int


class SkuAdminDetail(BaseModel):
    """GET /v1/admin/skus/{id} — full SKU info + per-node breakdown.

    ``display_name`` mirrors the value on ``SkuAdminItem``.
    ``sales_30d_count`` / ``sales_30d_revenue`` aggregate committed
    and expired orders over the trailing 30 days (excludes reserved
    and released — reserved isn't paid yet, released was refunded).
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    id: int
    code: str
    product_kind: str
    geo_code: str
    protocol: str
    duration_days: int
    price_per_piece: Decimal | None = None
    price_per_gb: Decimal | None = None
    target_stock: int
    refill_batch_size: int
    validation_require_ipv6: bool
    is_active: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    stock_total: dict[str, int]
    stock_breakdown: list[SkuStockBreakdownItem]
    display_name: str
    sales_30d_count: int = 0
    sales_30d_revenue: Decimal = Field(default_factory=lambda: Decimal("0"))


class SkuCreateRequest(BaseModel):
    """POST /v1/admin/skus body."""

    model_config = _API_MODEL_CONFIG

    code: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9_]+$")
    product_kind: str = Field(min_length=1, max_length=32)
    geo_code: str = Field(default="", max_length=8)
    protocol: str = Field(min_length=1, max_length=16)
    duration_days: int = Field(default=30, ge=1, le=365)
    price_per_piece: Decimal | None = None
    price_per_gb: Decimal | None = None
    target_stock: int = Field(ge=1, le=1_000_000)
    refill_batch_size: int = Field(default=500, ge=1, le=1_000_000)
    validation_require_ipv6: bool = True
    is_active: bool = True

    @field_validator("price_per_piece", "price_per_gb", mode="before")
    @classmethod
    def _parse_decimal(cls, v: Any) -> Any:
        return _coerce_decimal(v)

    @field_validator("price_per_piece", "price_per_gb")
    @classmethod
    def _positive_bounded(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return v
        if v <= 0:
            raise ValueError("price must be > 0")
        if v > Decimal("10000"):
            raise ValueError("price must be <= 10000")
        return v

    @model_validator(mode="after")
    def _check_price_kind_match(self) -> SkuCreateRequest:
        if self.product_kind == "datacenter_pergb":
            if self.price_per_gb is None:
                raise ValueError("price_per_gb required for datacenter_pergb")
        else:
            if self.price_per_piece is None:
                raise ValueError("price_per_piece required for per-piece SKU")
        return self


class SkuUpdateRequest(BaseModel):
    """PATCH /v1/admin/skus/{id} body — all fields optional."""

    model_config = _API_MODEL_CONFIG

    price_per_piece: Decimal | None = None
    price_per_gb: Decimal | None = None
    target_stock: int | None = Field(default=None, ge=1, le=1_000_000)
    refill_batch_size: int | None = Field(default=None, ge=1, le=1_000_000)
    duration_days: int | None = Field(default=None, ge=1, le=365)
    validation_require_ipv6: bool | None = None
    is_active: bool | None = None

    @field_validator("price_per_piece", "price_per_gb", mode="before")
    @classmethod
    def _parse_decimal(cls, v: Any) -> Any:
        return _coerce_decimal(v)

    @field_validator("price_per_piece", "price_per_gb")
    @classmethod
    def _positive_bounded(cls, v: Decimal | None) -> Decimal | None:
        if v is None:
            return v
        if v <= 0:
            raise ValueError("price must be > 0")
        if v > Decimal("10000"):
            raise ValueError("price must be <= 10000")
        return v


class BindingItem(BaseModel):
    """One row in GET /v1/admin/skus/{id}/bindings.

    ``available_count`` (D-Polishing-A.4) is the count of
    ``proxy_inventory`` rows with ``status='available'`` for this
    (sku_id, node_id) pair — the bot now renders it directly instead
    of looking it up from ``sku.stock_breakdown`` after-the-fact.
    Default 0 keeps backward compat with single-row INSERT paths
    (POST + PATCH) that build the response without the JOIN.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    node_id: str
    node_name: str
    node_geo: str
    weight: int
    max_batch_size: int
    is_active: bool
    available_count: int = 0
    created_at: datetime
    updated_at: datetime


class BindingListResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    items: list[BindingItem]


class BindingCreateRequest(BaseModel):
    """POST /v1/admin/skus/{id}/bindings body."""

    model_config = _API_MODEL_CONFIG

    node_id: str = Field(min_length=1, max_length=64)
    weight: int = Field(default=100, ge=0, le=10_000)
    max_batch_size: int = Field(default=1500, ge=1, le=1_000_000)


class BindingUpdateRequest(BaseModel):
    """PATCH /v1/admin/skus/{id}/bindings/{node_id} body."""

    model_config = _API_MODEL_CONFIG

    weight: int | None = Field(default=None, ge=0, le=10_000)
    max_batch_size: int | None = Field(default=None, ge=1, le=1_000_000)
    is_active: bool | None = None


class PergbTierItem(BaseModel):
    """One pergb tier row (sku_tiers table)."""

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


class PergbTiersResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    items: list[PergbTierItem]


class PergbTiersPutRequest(BaseModel):
    """PUT /v1/admin/skus/{id}/tiers — atomic replace."""

    model_config = _API_MODEL_CONFIG

    tiers: list[PergbTierItem] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _check_monotonicity(self) -> PergbTiersPutRequest:
        prev_gb = 0
        prev_price: Decimal | None = None
        for tier in self.tiers:
            if tier.gb <= prev_gb:
                raise ValueError("gb_brackets must be strictly ascending")
            if prev_price is not None and tier.price_per_gb > prev_price:
                raise ValueError("price_per_gb must be monotonically non-increasing")
            prev_gb = tier.gb
            prev_price = tier.price_per_gb
        return self


class GeoUsageItem(BaseModel):
    model_config = _API_MODEL_CONFIG

    geo_code: str
    sku_count: int
    active_count: int = 0  # D-Polishing-A.3 — count of is_active=true SKUs


class GeoListResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    items: list[GeoUsageItem]


# === /v1/admin/geos/catalog + geo CRUD (PROXY-PARITY-1 Phase A) ===
#
# Distinct from GeoUsageItem / GeoListResponse above (those back the
# legacy GET /geos usage-count endpoint). These carry the full geo
# DISPLAY metadata rows from the ``geos`` table.


class GeoCatalogItem(BaseModel):
    model_config = _API_MODEL_CONFIG

    code: str
    flag: str
    name_ru: str
    name_en: str | None = None
    sort_order: int = 0
    is_active: bool = True
    sku_count: int = 0  # COUNT of skus carrying this geo_code (0 ok)


class GeoCatalogListResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    items: list[GeoCatalogItem]


class GeoCreateRequest(BaseModel):
    model_config = _API_MODEL_CONFIG

    code: str = Field(min_length=2, max_length=8)
    flag: str = Field(default="🌐", min_length=1, max_length=16)
    name_ru: str = Field(min_length=1, max_length=64)
    name_en: str | None = Field(default=None, max_length=64)
    sort_order: int = Field(default=0, ge=0, le=100_000)
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def _normalize_code(cls, v: str) -> str:
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z]{2,8}", v):
            raise ValueError("code must be 2-8 uppercase letters A-Z")
        return v


class GeoUpdateRequest(BaseModel):
    """Partial update — every field optional. ``model_dump(exclude_none
    =True)`` yields only the columns the caller actually sent (``False``
    / ``0`` are kept; only ``None`` is dropped)."""

    model_config = _API_MODEL_CONFIG

    flag: str | None = Field(default=None, min_length=1, max_length=16)
    name_ru: str | None = Field(default=None, min_length=1, max_length=64)
    name_en: str | None = Field(default=None, max_length=64)
    sort_order: int | None = Field(default=None, ge=0, le=100_000)
    is_active: bool | None = None


class ProductKindItem(BaseModel):
    model_config = _API_MODEL_CONFIG

    kind: str
    name: str
    sku_count: int
    total_stock: int = 0  # D-Polishing-A.3 — SUM of stock_available
    #   across all SKUs of this kind


class ProductKindListResponse(BaseModel):
    model_config = _API_MODEL_CONFIG

    items: list[ProductKindItem]
