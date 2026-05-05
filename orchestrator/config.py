import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    env_path = Path(os.getenv("ORCHESTRATOR_ENV_FILE", PROJECT_ROOT / ".env"))
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    api_key: str
    host: str
    port: int
    database_url: str
    jobs_root: Path
    node_request_timeout_sec: int
    start_port_min: int
    start_port_max: int
    worker_poll_interval_sec: int
    refill_interval_sec: int
    refill_default_priority: int
    refill_max_skus_per_cycle: int
    proxy_allow_degraded_nodes: bool
    validation_batch_size: int
    validation_concurrency: int
    validation_poll_interval_sec: int
    redis_url: str
    reservation_default_ttl_sec: int
    reservation_min_ttl_sec: int
    reservation_max_ttl_sec: int
    watchdog_interval_sec: int
    watchdog_running_timeout_sec: int
    watchdog_pending_validation_timeout_sec: int
    validation_strict_ssl: bool
    traffic_poll_interval_sec: int
    traffic_poll_min_interval_sec: int
    traffic_poll_request_timeout_sec: int
    traffic_poll_degrade_after: int


def get_config() -> Config:
    _load_dotenv()
    return Config(
        api_key=os.getenv("ORCHESTRATOR_API_KEY", "").strip(),
        host=os.getenv("ORCHESTRATOR_HOST", "0.0.0.0").strip(),
        port=_int_env("ORCHESTRATOR_PORT", 8090),
        database_url=os.getenv("DATABASE_URL", "").strip(),
        jobs_root=Path(os.getenv("JOBS_ROOT", "/opt/netrun-orchestrator/jobs")).resolve(),
        node_request_timeout_sec=_int_env("NODE_REQUEST_TIMEOUT_SEC", 1200),
        start_port_min=_int_env("ORCHESTRATOR_START_PORT_MIN", 32000),
        start_port_max=_int_env("ORCHESTRATOR_START_PORT_MAX", 65000),
        worker_poll_interval_sec=_int_env("WORKER_POLL_INTERVAL_SEC", 2),
        refill_interval_sec=_int_env("PROXY_REFILL_INTERVAL_SEC", 30),
        refill_default_priority=_int_env("REFILL_DEFAULT_PRIORITY", 10),
        refill_max_skus_per_cycle=_int_env("REFILL_MAX_SKUS_PER_CYCLE", 100),
        proxy_allow_degraded_nodes=_bool_env("PROXY_ALLOW_DEGRADED_NODES", False),
        validation_batch_size=_int_env("VALIDATION_BATCH_SIZE", 50),
        validation_concurrency=_int_env("VALIDATION_CONCURRENCY", 20),
        validation_poll_interval_sec=_int_env("VALIDATION_POLL_INTERVAL_SEC", 5),
        redis_url=os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0").strip(),
        reservation_default_ttl_sec=_int_env("RESERVATION_DEFAULT_TTL_SEC", 300),
        reservation_min_ttl_sec=_int_env("RESERVATION_MIN_TTL_SEC", 30),
        reservation_max_ttl_sec=_int_env("RESERVATION_MAX_TTL_SEC", 3600),
        watchdog_interval_sec=_int_env("WATCHDOG_INTERVAL_SEC", 60),
        watchdog_running_timeout_sec=_int_env("WATCHDOG_RUNNING_TIMEOUT_SEC", 1800),
        watchdog_pending_validation_timeout_sec=_int_env("WATCHDOG_PENDING_VALIDATION_TIMEOUT_SEC", 600),
        validation_strict_ssl=_bool_env("VALIDATION_STRICT_SSL", True),
        traffic_poll_interval_sec=_int_env("TRAFFIC_POLL_INTERVAL_SEC", 60),
        traffic_poll_min_interval_sec=_int_env("TRAFFIC_POLL_MIN_INTERVAL_SEC", 30),
        traffic_poll_request_timeout_sec=_int_env("TRAFFIC_POLL_REQUEST_TIMEOUT_SEC", 10),
        traffic_poll_degrade_after=_int_env("TRAFFIC_POLL_DEGRADE_AFTER", 5),
    )
