"""Pydantic v2 schemas for orchestrator DB rows and API contracts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# === Enums ===


class NodeStatus(str, Enum):
    UNKNOWN = "unknown"
    READY = "ready"
    UNAVAILABLE = "unavailable"


class NodeRuntimeStatus(str, Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    DISABLED = "disabled"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class JobReason(str, Enum):
    MANUAL = "manual"
    REFILL = "refill"
    API = "api"
    ADMIN = "admin"


class ProductKind(str, Enum):
    IPV6 = "ipv6"
    DATACENTER_PERGB = "datacenter_pergb"


class Protocol(str, Enum):
    SOCKS5 = "socks5"
    HTTP = "http"


class PortAllocationStatus(str, Enum):
    RESERVED = "reserved"
    RELEASED = "released"


class ProxyInventoryStatus(str, Enum):
    PENDING_VALIDATION = "pending_validation"
    AVAILABLE = "available"
    RESERVED = "reserved"
    SOLD = "sold"
    EXPIRED_GRACE = "expired_grace"
    ARCHIVED = "archived"
    INVALID = "invalid"
    ALLOCATED_PERGB = "allocated_pergb"


class TrafficAccountStatus(str, Enum):
    ACTIVE = "active"
    DEPLETED = "depleted"
    EXPIRED = "expired"
    ARCHIVED = "archived"


class OrderStatus(str, Enum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    EXPIRED = "expired"


class DeliveryFormat(str, Enum):
    SOCKS5_URI = "socks5_uri"
    HOST_PORT_USER_PASS = "host_port_user_pass"
    USER_PASS_AT_HOST_PORT = "user_pass_at_host_port"
    JSON = "json"


# === Models ===

_DB_MODEL_CONFIG = ConfigDict(from_attributes=True, str_strip_whitespace=True)


class Node(BaseModel):
    model_config = _DB_MODEL_CONFIG

    id: str
    name: str
    url: str
    geo: str = ""
    status: NodeStatus = NodeStatus.UNKNOWN
    capacity: int = Field(gt=0)
    api_key: str | None = None
    last_health_check: datetime | None = None
    weight: int = 100
    max_parallel_jobs: int = 1
    max_batch_size: int = 1500
    runtime_status: NodeRuntimeStatus = NodeRuntimeStatus.ACTIVE
    heartbeat_failures: int = 0
    last_heartbeat_at: datetime | None = None
    generator_script: str | None = None
    generator_args_template: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class Job(BaseModel):
    model_config = _DB_MODEL_CONFIG

    id: str
    status: JobStatus
    count: int = Field(gt=0)
    product: str
    node_id: str | None = None
    start_port: int | None = None
    profile: dict[str, Any]
    result_path: str | None = None
    error: str | None = None
    idempotency_key: str | None = None
    sku_id: int | None = None
    reason: JobReason = JobReason.MANUAL
    priority: int = 10
    attempts: int = 0
    max_attempts: int = 5
    payload: dict[str, Any] = Field(default_factory=dict)
    locked_by: str | None = None
    locked_at: datetime | None = None
    available_at: datetime
    created_at: datetime
    updated_at: datetime


class Sku(BaseModel):
    model_config = _DB_MODEL_CONFIG

    id: int
    code: str
    product_kind: ProductKind
    geo_code: str
    protocol: Protocol
    duration_days: int = 30
    price_per_piece: Decimal | None = None
    price_per_gb: Decimal | None = None
    target_stock: int = 0
    refill_batch_size: int = 500
    validation_require_ipv6: bool = True
    is_active: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class SkuNodeBinding(BaseModel):
    model_config = _DB_MODEL_CONFIG

    id: int
    sku_id: int
    node_id: str
    weight: int = 100
    max_batch_size: int = 1500
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


class NodePortAllocation(BaseModel):
    model_config = _DB_MODEL_CONFIG

    id: int
    job_id: str
    node_id: str
    start_port: int
    end_port: int
    proxy_count: int
    status: PortAllocationStatus = PortAllocationStatus.RESERVED
    released_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProxyInventory(BaseModel):
    model_config = _DB_MODEL_CONFIG

    id: int
    sku_id: int
    node_id: str
    generation_job_id: str | None = None
    login: str
    password: str
    host: str
    port: int
    status: ProxyInventoryStatus = ProxyInventoryStatus.PENDING_VALIDATION
    reservation_key: str | None = None
    reserved_at: datetime | None = None
    order_id: int | None = None
    sold_at: datetime | None = None
    expires_at: datetime | None = None
    archived_at: datetime | None = None
    external_ip: str | None = None
    geo_country: str | None = None
    geo_city: str | None = None
    latency_ms: int | None = None
    ipv6_only: bool | None = None
    dns_sanity: bool | None = None
    validation_error: str | None = None
    validated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class Order(BaseModel):
    model_config = _DB_MODEL_CONFIG

    id: int
    order_ref: str
    user_id: int
    sku_id: int
    status: OrderStatus
    requested_count: int
    allocated_count: int = 0
    reservation_key: str
    reserved_at: datetime
    expires_at: datetime
    committed_at: datetime | None = None
    released_at: datetime | None = None
    proxies_expires_at: datetime | None = None
    price_amount: Decimal | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class DeliveryFile(BaseModel):
    model_config = _DB_MODEL_CONFIG

    id: int
    order_id: int
    format: DeliveryFormat
    line_count: int
    checksum_sha256: str
    content: str | None = None
    content_expires_at: datetime
    created_at: datetime
